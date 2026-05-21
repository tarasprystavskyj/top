#!/usr/bin/env python3
"""
Match TradingView backtest trades against BingX real trades, persist results to SQLite,
compute slippage statistics, and generate a 2-panel comparison canvas.

Typical usage:

python match_trades_tv_bingx.py \
  --tv "C_-_limit_14_SHORT_(EVEN)_bingX_COMP_[3m_cap_fixed_+_pack]_BINGX_COMPUSDT.P_2026-04-13_c7112.csv" \
  --bingx "Результат COMP C short even v63 - 2026-04-10 till 04-13 08_57.csv" \
  --db comparison_results.sqlite \
  --out-dir out \
  --label comp_short_v63

Notes:
- BingX exports often come either as UTF-8 or CP1251. This script auto-tries both.
- TradingView export rows are packed by exact timestamp + side before matching.
- TV rows with Signal == "Open" are excluded from matching and TV realized PNL by default,
  because they are usually forced end-of-backtest closes rather than strategy signals.
- Matching is approximate and optimized by time first, then quantity, then price.
- Signed slippage is defined so that POSITIVE is favorable for a short strategy:
    * short entry: real_price > tv_price  => positive
    * short exit:  real_price < tv_price  => positive
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

try:
    from scipy.optimize import linear_sum_assignment
    HAVE_SCIPY = True
except Exception:  # pragma: no cover
    HAVE_SCIPY = False
    linear_sum_assignment = None


TV_REQUIRED_COLS = {
    "Trade #",
    "Type",
    "Date and time",
    "Signal",
    "Price USDT",
    "Size (qty)",
    "Net P&L USDT",
}

BINGX_REQUIRED_COLS = {
    "Час виконання",
    "Ф’ючерси / Напрямок",
    "Виконано",
    "Ціна виконання",
    "Закриті PnL / %",
    "Комісія",
    "Ордер №",
}


MATCHED_ORDER_COLUMNS = [
    "side",
    "real_time",
    "tv_time",
    "time_diff_min",
    "real_qty",
    "tv_qty",
    "qty_diff",
    "real_price",
    "tv_price",
    "price_diff",
    "signed_slippage_pct",
    "abs_slippage_pct",
    "signed_slippage_bps",
    "abs_slippage_bps",
    "real_closed_pnl",
    "real_fee",
    "real_net_pnl",
    "tv_net_pnl",
    "tv_signal",
    "tv_trade_ids",
    "confidence",
    "real_order_no",
]


@dataclass
class MatchConfig:
    tolerance_min: float = 10.0
    qty_unit: float = 0.09
    w_time: float = 1.0
    w_qty: float = 4.0
    w_price: float = 20.0
    exclude_open_eod: bool = True


# ---------- Generic helpers ----------

def read_csv_auto(path: Path) -> pd.DataFrame:
    """Read CSV with a small encoding fallback ladder."""
    encodings = ["utf-8-sig", "utf-8", "cp1251", "cp1252", "latin1"]
    last_exc: Optional[Exception] = None
    for enc in encodings:
        try:
            return pd.read_csv(path, encoding=enc)
        except Exception as exc:  # pragma: no cover - best effort
            last_exc = exc
    raise RuntimeError(f"Could not read CSV: {path}\nLast error: {last_exc}")


def require_columns(df: pd.DataFrame, required: set[str], file_label: str) -> None:
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError(f"{file_label}: missing required columns: {missing}")


def parse_datetime_series(series: pd.Series) -> pd.Series:
    """Parse the known BingX date variants first, then fall back."""
    s = series.astype(str).str.strip()
    out = pd.to_datetime(s, format="%m/%d/%y %I:%M %p", errors="coerce")
    mask = out.isna()
    if mask.any():
        out.loc[mask] = pd.to_datetime(s.loc[mask], format="%d/%m/%y %H:%M", errors="coerce")
    mask = out.isna()
    if mask.any():
        out.loc[mask] = pd.to_datetime(s.loc[mask], dayfirst=True, errors="coerce")
    return out


def extract_first_float(series: pd.Series, pattern: str) -> pd.Series:
    return (
        series.astype(str)
        .str.replace("−", "-", regex=False)
        .str.replace(",", ".", regex=False)
        .str.extract(pattern)[0]
        .astype(float)
    )


def safe_stem(text: str) -> str:
    out = []
    for ch in text:
        if ch.isalnum() or ch in ("-", "_"):
            out.append(ch)
        else:
            out.append("_")
    return "".join(out).strip("_") or "comparison"


# ---------- Parsing ----------

def parse_tv_csv(path: Path) -> pd.DataFrame:
    df = read_csv_auto(path)
    require_columns(df, TV_REQUIRED_COLS, "TradingView CSV")

    df = df.copy()
    df["dt"] = pd.to_datetime(df["Date and time"], errors="coerce")
    if df["dt"].isna().any():
        bad = int(df["dt"].isna().sum())
        raise ValueError(f"TradingView CSV: could not parse {bad} timestamps")

    numeric_cols = ["Price USDT", "Size (qty)", "Net P&L USDT"]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    if df[numeric_cols].isna().any().any():
        raise ValueError("TradingView CSV: some numeric fields could not be parsed")

    df["side"] = df["Type"].astype(str)
    return df


def parse_bingx_csv(path: Path) -> pd.DataFrame:
    df = read_csv_auto(path)
    require_columns(df, BINGX_REQUIRED_COLS, "BingX CSV")

    # Remove trailing unnamed columns sometimes present in CP1251 exports
    unnamed = [c for c in df.columns if str(c).startswith("Unnamed:")]
    if unnamed:
        df = df.drop(columns=unnamed)

    df = df.copy()
    df["dt"] = parse_datetime_series(df["Час виконання"])
    if df["dt"].isna().any():
        bad = int(df["dt"].isna().sum())
        raise ValueError(f"BingX CSV: could not parse {bad} timestamps")

    df["qty"] = extract_first_float(df["Виконано"], r"([\d.]+)")
    df["price"] = extract_first_float(df["Ціна виконання"], r"([\d.]+)")
    df["closed_pnl"] = extract_first_float(df["Закриті PnL / %"], r"([−-]?[\d.]+)\s*\u00a0*\u00a0*USDT")
    df["fee"] = extract_first_float(df["Комісія"], r"([−-]?[\d.]+)")
    df["side"] = np.where(df["Ф’ючерси / Напрямок"].astype(str).str.contains("Відкрити"), "Entry short", "Exit short")
    df["net_pnl"] = df["closed_pnl"] + df["fee"]
    df["order_no_str"] = df["Ордер №"].astype(str).str.strip()

    needed = ["qty", "price", "closed_pnl", "fee"]
    if df[needed].isna().any().any():
        raise ValueError("BingX CSV: some numeric fields could not be parsed")

    return df


# ---------- Packing and matching ----------

def pack_tv_orders(tv_df: pd.DataFrame, real_df: pd.DataFrame, config: MatchConfig) -> pd.DataFrame:
    start = real_df["dt"].min() - pd.Timedelta(minutes=5)
    end = real_df["dt"].max() + pd.Timedelta(minutes=5)

    mask = (tv_df["dt"] >= start) & (tv_df["dt"] <= end)
    if config.exclude_open_eod:
        mask &= tv_df["Signal"].astype(str).ne("Open")

    df = tv_df.loc[mask].copy().sort_values("dt")
    packed = (
        df.groupby(["dt", "Type"], as_index=False)
        .agg(
            tv_price=("Price USDT", "mean"),
            tv_qty=("Size (qty)", "sum"),
            tv_units=("Trade #", "count"),
            tv_trade_ids=("Trade #", lambda s: ",".join(map(str, s.tolist()))),
            tv_signal=("Signal", lambda s: s.iloc[0]),
            tv_net_pnl=("Net P&L USDT", "sum"),
        )
        .rename(columns={"Type": "side"})
    )
    return packed


def greedy_assignment(cost: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Fallback if scipy is unavailable."""
    cost = cost.copy()
    rows: list[int] = []
    cols: list[int] = []
    used_r: set[int] = set()
    used_c: set[int] = set()

    flat = [(float(cost[i, j]), i, j) for i in range(cost.shape[0]) for j in range(cost.shape[1])]
    flat.sort(key=lambda x: x[0])
    for c, i, j in flat:
        if not math.isfinite(c) or c >= 1e6:
            break
        if i in used_r or j in used_c:
            continue
        used_r.add(i)
        used_c.add(j)
        rows.append(i)
        cols.append(j)
    return np.array(rows), np.array(cols)


def match_orders(real_df: pd.DataFrame, tv_packed: pd.DataFrame, config: MatchConfig) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    matches: list[dict] = []
    matched_real_rows: set[int] = set()
    matched_tv_rows: set[int] = set()

    for side in ["Entry short", "Exit short"]:
        real_side = (
            real_df[real_df["side"] == side]
            .copy()
            .sort_values("dt")
            .reset_index()
            .rename(columns={"index": "real_row"})
        )
        tv_side = (
            tv_packed[tv_packed["side"] == side]
            .copy()
            .sort_values("dt")
            .reset_index()
            .rename(columns={"index": "tv_row"})
        )

        n, m = len(real_side), len(tv_side)
        if n == 0 or m == 0:
            continue

        cost = np.full((n, m), 1e6, dtype=float)
        for i, row in real_side.iterrows():
            time_diff_min = (tv_side["dt"] - row["dt"]).abs().dt.total_seconds() / 60.0
            qty_diff_units = (tv_side["tv_qty"] - row["qty"]).abs() / config.qty_unit
            price_diff = (tv_side["tv_price"] - row["price"]).abs()

            c = (
                config.w_time * time_diff_min
                + config.w_qty * qty_diff_units
                + config.w_price * price_diff
            )
            c[time_diff_min > config.tolerance_min] = 1e6
            cost[i, :] = c.values

        if HAVE_SCIPY:
            real_idx, tv_idx = linear_sum_assignment(cost)
        else:  # pragma: no cover
            real_idx, tv_idx = greedy_assignment(cost)

        for i, j in zip(real_idx, tv_idx):
            if cost[i, j] >= 1e6:
                continue

            rr = real_side.iloc[i]
            tt = tv_side.iloc[j]
            time_diff_min = abs((tt["dt"] - rr["dt"]).total_seconds()) / 60.0
            qty_diff = float(tt["tv_qty"] - rr["qty"])
            price_diff = float(tt["tv_price"] - rr["price"])

            confidence = "low"
            if time_diff_min <= 1 and abs(qty_diff) < 1e-9 and abs(price_diff) <= 0.03:
                confidence = "high"
            elif time_diff_min <= 3 and abs(qty_diff) <= config.qty_unit + 1e-9 and abs(price_diff) <= 0.06:
                confidence = "medium"

            # Positive signed slippage means favorable for the short strategy.
            if side == "Entry short":
                signed_slip_pct = (rr["price"] - tt["tv_price"]) / tt["tv_price"] * 100.0
            else:
                signed_slip_pct = (tt["tv_price"] - rr["price"]) / tt["tv_price"] * 100.0
            abs_slip_pct = abs(rr["price"] - tt["tv_price"]) / tt["tv_price"] * 100.0
            signed_slip_bps = signed_slip_pct * 100.0
            abs_slip_bps = abs_slip_pct * 100.0

            matches.append(
                {
                    "side": side,
                    "real_time": rr["dt"],
                    "tv_time": tt["dt"],
                    "time_diff_min": round(time_diff_min, 6),
                    "real_qty": float(rr["qty"]),
                    "tv_qty": float(tt["tv_qty"]),
                    "qty_diff": round(qty_diff, 6),
                    "real_price": float(rr["price"]),
                    "tv_price": float(tt["tv_price"]),
                    "price_diff": round(price_diff, 6),
                    "signed_slippage_pct": signed_slip_pct,
                    "abs_slippage_pct": abs_slip_pct,
                    "signed_slippage_bps": signed_slip_bps,
                    "abs_slippage_bps": abs_slip_bps,
                    "real_closed_pnl": float(rr["closed_pnl"]),
                    "real_fee": float(rr["fee"]),
                    "real_net_pnl": float(rr["net_pnl"]),
                    "tv_net_pnl": float(tt["tv_net_pnl"]),
                    "tv_signal": str(tt["tv_signal"]),
                    "tv_trade_ids": str(tt["tv_trade_ids"]),
                    "confidence": confidence,
                    "real_order_no": str(rr["order_no_str"]),
                }
            )
            matched_real_rows.add(int(rr["real_row"]))
            matched_tv_rows.add(int(tt["tv_row"]))

    matched_df = pd.DataFrame(matches).sort_values(["real_time", "side"]).reset_index(drop=True)
    unmatched_real = real_df.loc[~real_df.index.isin(matched_real_rows)].copy().sort_values("dt")
    unmatched_tv = tv_packed.loc[~tv_packed.index.isin(matched_tv_rows)].copy().sort_values("dt")
    return matched_df, unmatched_real, unmatched_tv


# ---------- Stats ----------

def build_summary(real_df: pd.DataFrame, tv_packed: pd.DataFrame, matched_df: pd.DataFrame, tv_df: pd.DataFrame, config: MatchConfig) -> pd.DataFrame:
    start = real_df["dt"].min()
    end = real_df["dt"].max()

    tv_realized = tv_df[(tv_df["dt"] >= start) & (tv_df["dt"] <= end) & (tv_df["Type"] == "Exit short")].copy()
    if config.exclude_open_eod:
        tv_realized = tv_realized[tv_realized["Signal"] != "Open"]

    rows = [
        {"metric": "Real orders", "value": float(len(real_df))},
        {"metric": "TV packed rows", "value": float(len(tv_packed))},
        {"metric": "Matched rows", "value": float(len(matched_df))},
        {"metric": "Matched share of real orders", "value": float(len(matched_df) / len(real_df)) if len(real_df) else np.nan},
        {"metric": "Matched share of TV packed rows", "value": float(len(matched_df) / len(tv_packed)) if len(tv_packed) else np.nan},
        {"metric": "High confidence matches", "value": float((matched_df["confidence"] == "high").sum()) if len(matched_df) else 0.0},
        {"metric": "Medium confidence matches", "value": float((matched_df["confidence"] == "medium").sum()) if len(matched_df) else 0.0},
        {"metric": "Low confidence matches", "value": float((matched_df["confidence"] == "low").sum()) if len(matched_df) else 0.0},
        {"metric": "Mean time diff (min)", "value": float(matched_df["time_diff_min"].mean()) if len(matched_df) else np.nan},
        {"metric": "Median time diff (min)", "value": float(matched_df["time_diff_min"].median()) if len(matched_df) else np.nan},
        {"metric": "Mean abs slippage (bps)", "value": float(matched_df["abs_slippage_bps"].mean()) if len(matched_df) else np.nan},
        {"metric": "Median abs slippage (bps)", "value": float(matched_df["abs_slippage_bps"].median()) if len(matched_df) else np.nan},
        {"metric": "Mean signed slippage (bps)", "value": float(matched_df["signed_slippage_bps"].mean()) if len(matched_df) else np.nan},
        {"metric": "TV overlap realized PNL (USDT)", "value": float(tv_realized["Net P&L USDT"].sum())},
        {"metric": "BingX net PNL (USDT)", "value": float(real_df["net_pnl"].sum())},
    ]
    return pd.DataFrame(rows)


def build_slippage_breakdown(matched_df: pd.DataFrame) -> pd.DataFrame:
    if matched_df.empty:
        return pd.DataFrame(columns=["group", "n", "mean_abs_bps", "median_abs_bps", "mean_signed_bps"])

    parts = []
    for side, sdf in matched_df.groupby("side"):
        parts.append(
            {
                "group": side,
                "n": len(sdf),
                "mean_abs_bps": sdf["abs_slippage_bps"].mean(),
                "median_abs_bps": sdf["abs_slippage_bps"].median(),
                "mean_signed_bps": sdf["signed_slippage_bps"].mean(),
            }
        )
    for conf, sdf in matched_df.groupby("confidence"):
        parts.append(
            {
                "group": f"confidence:{conf}",
                "n": len(sdf),
                "mean_abs_bps": sdf["abs_slippage_bps"].mean(),
                "median_abs_bps": sdf["abs_slippage_bps"].median(),
                "mean_signed_bps": sdf["signed_slippage_bps"].mean(),
            }
        )
    return pd.DataFrame(parts)


# ---------- SQLite ----------

def save_to_sqlite(
    db_path: Path,
    label: str,
    tv_df: pd.DataFrame,
    real_df: pd.DataFrame,
    tv_packed: pd.DataFrame,
    matched_df: pd.DataFrame,
    unmatched_real: pd.DataFrame,
    unmatched_tv: pd.DataFrame,
    summary_df: pd.DataFrame,
    slippage_df: pd.DataFrame,
    config: MatchConfig,
    tv_path: Path,
    bingx_path: Path,
) -> int:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS comparison_runs (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT,
                created_at TEXT,
                tv_path TEXT,
                bingx_path TEXT,
                params_json TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO comparison_runs(label, created_at, tv_path, bingx_path, params_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                label,
                datetime.utcnow().isoformat(timespec="seconds") + "Z",
                str(tv_path),
                str(bingx_path),
                json.dumps(config.__dict__, ensure_ascii=False),
            ),
        )
        run_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

        def push(df: pd.DataFrame, table: str) -> None:
            tmp = df.copy()
            tmp.insert(0, "run_id", run_id)
            for col in tmp.columns:
                if pd.api.types.is_datetime64_any_dtype(tmp[col]):
                    tmp[col] = tmp[col].dt.strftime("%Y-%m-%d %H:%M:%S")
            tmp.to_sql(table, conn, if_exists="append", index=False)

        push(tv_df, "tv_orders")
        push(real_df, "real_orders")
        push(tv_packed, "tv_packed_orders")
        push(matched_df, "order_matches")
        push(unmatched_real, "unmatched_real_orders")
        push(unmatched_tv, "unmatched_tv_packs")
        push(summary_df, "comparison_summary")
        push(slippage_df, "slippage_breakdown")
        conn.commit()
        return run_id
    finally:
        conn.close()


# ---------- Plotting ----------

def make_canvas(
    tv_df: pd.DataFrame,
    real_df: pd.DataFrame,
    matched_df: pd.DataFrame,
    config: MatchConfig,
    out_path: Path,
) -> None:
    start = real_df["dt"].min()
    end = real_df["dt"].max()

    tv_line = tv_df[(tv_df["dt"] >= start) & (tv_df["dt"] <= end) & (tv_df["Type"] == "Exit short")].copy()
    if config.exclude_open_eod:
        tv_line = tv_line[tv_line["Signal"] != "Open"]
    tv_line = tv_line[["dt", "Net P&L USDT"]].sort_values("dt")
    tv_line["cum_pnl"] = tv_line["Net P&L USDT"].cumsum()

    real_line = real_df[["dt", "net_pnl"]].sort_values("dt").copy()
    real_line["cum_pnl"] = real_line["net_pnl"].cumsum()

    slip_line = matched_df.sort_values("real_time").copy()
    if not slip_line.empty:
        slip_line["rolling_signed_bps_15"] = slip_line["signed_slippage_bps"].rolling(15, min_periods=3).mean()

    fig, axes = plt.subplots(2, 1, figsize=(13, 9), sharex=False)

    axes[0].plot(tv_line["dt"], tv_line["cum_pnl"], label="TV backtest cumulative net PNL")
    axes[0].plot(real_line["dt"], real_line["cum_pnl"], label="BingX actual cumulative net PNL")
    axes[0].set_title("Cumulative net PNL: TradingView vs BingX")
    axes[0].set_xlabel("Time")
    axes[0].set_ylabel("Cumulative PNL (USDT)")
    axes[0].grid(True, alpha=0.3)
    axes[0].legend()

    if not slip_line.empty:
        axes[1].plot(slip_line["real_time"], slip_line["signed_slippage_bps"], label="Signed slippage (bps)")
        axes[1].plot(slip_line["real_time"], slip_line["rolling_signed_bps_15"], label="Rolling mean 15 matches")
    axes[1].axhline(0.0, linewidth=1)
    axes[1].set_title("Signed slippage for matched orders (positive = favorable for short)")
    axes[1].set_xlabel("Real execution time")
    axes[1].set_ylabel("Slippage (bps)")
    axes[1].grid(True, alpha=0.3)
    axes[1].legend()

    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_path, dpi=160, bbox_inches="tight")
    plt.close(fig)


# ---------- CLI ----------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Match TradingView backtest trades to BingX real trades")
    p.add_argument("--tv", required=True, help="Path to TradingView CSV")
    p.add_argument("--bingx", required=True, help="Path to BingX CSV")
    p.add_argument("--db", default="comparison_results.sqlite", help="SQLite DB path")
    p.add_argument("--out-dir", default=".", help="Directory for CSV/PNG outputs")
    p.add_argument("--label", default=None, help="Run label; default is derived from filenames")
    p.add_argument("--tolerance-min", type=float, default=10.0, help="Max allowed time difference per match")
    p.add_argument("--qty-unit", type=float, default=0.09, help="Base order size unit used in qty penalty")
    p.add_argument("--w-time", type=float, default=1.0, help="Weight for time difference in matching cost")
    p.add_argument("--w-qty", type=float, default=4.0, help="Weight for quantity difference in matching cost")
    p.add_argument("--w-price", type=float, default=20.0, help="Weight for price difference in matching cost")
    p.add_argument("--include-open-eod", action="store_true", help="Include TV Signal=Open rows in matching and PNL")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    tv_path = Path(args.tv)
    bingx_path = Path(args.bingx)
    out_dir = Path(args.out_dir)
    db_path = Path(args.db)
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    label = args.label or safe_stem(f"{tv_path.stem}__{bingx_path.stem}")[:120]
    config = MatchConfig(
        tolerance_min=args.tolerance_min,
        qty_unit=args.qty_unit,
        w_time=args.w_time,
        w_qty=args.w_qty,
        w_price=args.w_price,
        exclude_open_eod=not args.include_open_eod,
    )

    tv_df = parse_tv_csv(tv_path)
    real_df = parse_bingx_csv(bingx_path)
    tv_packed = pack_tv_orders(tv_df, real_df, config)
    matched_df, unmatched_real, unmatched_tv = match_orders(real_df, tv_packed, config)
    summary_df = build_summary(real_df, tv_packed, matched_df, tv_df, config)
    slippage_df = build_slippage_breakdown(matched_df)

    prefix = out_dir / label
    matched_csv = Path(f"{prefix}_matched_orders.csv")
    unmatched_real_csv = Path(f"{prefix}_unmatched_real_orders.csv")
    unmatched_tv_csv = Path(f"{prefix}_unmatched_tv_packs.csv")
    summary_csv = Path(f"{prefix}_summary.csv")
    slippage_csv = Path(f"{prefix}_slippage_breakdown.csv")
    canvas_png = Path(f"{prefix}_canvas.png")

    matched_df.to_csv(matched_csv, index=False, encoding="utf-8-sig")
    unmatched_real.to_csv(unmatched_real_csv, index=False, encoding="utf-8-sig")
    unmatched_tv.to_csv(unmatched_tv_csv, index=False, encoding="utf-8-sig")
    summary_df.to_csv(summary_csv, index=False, encoding="utf-8-sig")
    slippage_df.to_csv(slippage_csv, index=False, encoding="utf-8-sig")
    make_canvas(tv_df, real_df, matched_df, config, canvas_png)

    run_id = save_to_sqlite(
        db_path=db_path,
        label=label,
        tv_df=tv_df,
        real_df=real_df,
        tv_packed=tv_packed,
        matched_df=matched_df,
        unmatched_real=unmatched_real,
        unmatched_tv=unmatched_tv,
        summary_df=summary_df,
        slippage_df=slippage_df,
        config=config,
        tv_path=tv_path,
        bingx_path=bingx_path,
    )

    print(f"run_id={run_id}")
    print(f"label={label}")
    print(f"db={db_path}")
    print(f"matched_csv={matched_csv}")
    print(f"unmatched_real_csv={unmatched_real_csv}")
    print(f"unmatched_tv_csv={unmatched_tv_csv}")
    print(f"summary_csv={summary_csv}")
    print(f"slippage_csv={slippage_csv}")
    print(f"canvas_png={canvas_png}")
    print(summary_df.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
