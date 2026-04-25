#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
monthly_akela_phase_proxybt.py

Monthly / rolling-month Akela ranking + close-only proxy short backtest.

Designed to work with:
  A) SQLite price_indicators DB:
     --db ../DB/combined_cache_3m_7200_500u.db

  B) Fast NPZ:
     --npz ../DB/combined_cache_3m_7200_500u.fast_ohlcv.npz

It imports rank_fast_cache_akela_phase_proxybt.py from the same directory and reuses:
  - load_yaml
  - load_fast_cache
  - compute_all

Outputs:
  1) --detail-out:
     per-month ranked rows for every symbol tested.

  2) --summary-out:
     symbol-level aggregation across months:
       months_tested
       positive_months
       positive_rate
       avg/median proxy returns
       median/max MDD
       avg final score
       latest month phase and BTC-relative weakness

  3) optional --converted-npz:
     if --db is provided, build NPZ first.

Important:
  This is close-only proxy backtest. For exact v5 strategy:
    use full OHLCV backtester on your server, because this script cannot model intrabar touch/TP/SL exactly.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import pandas as pd


def import_ranker(path: str):
    p = Path(path)
    if not p.exists():
        p = Path(__file__).resolve().parent / "rank_fast_cache_akela_phase_proxybt.py"
    if not p.exists():
        raise SystemExit(
            "rank_fast_cache_akela_phase_proxybt.py not found. "
            "Put it next to this script or pass --ranker-path."
        )
    spec = importlib.util.spec_from_file_location("ranker_phase_proxybt", str(p))
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def sqlite_header_check(db_path: str) -> dict:
    p = Path(db_path)
    b = p.read_bytes()[:100]
    if b[:16] != b"SQLite format 3\x00":
        return {"ok": False, "error": "not a SQLite 3 database header"}

    page_size = int.from_bytes(b[16:18], "big")
    if page_size == 1:
        page_size = 65536
    page_count = int.from_bytes(b[28:32], "big")
    expected = page_size * page_count
    actual = p.stat().st_size
    return {
        "ok": actual >= expected,
        "page_size": page_size,
        "page_count": page_count,
        "expected_bytes": expected,
        "actual_bytes": actual,
        "actual_expected_ratio": actual / expected if expected else None,
    }


def sqlite_quick_check(db_path: str) -> str:
    con = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    try:
        row = con.execute("PRAGMA quick_check").fetchone()
        return str(row[0]) if row else "no result"
    finally:
        con.close()


def db_columns(db_path: str, table: str = "price_indicators") -> list[str]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]
    finally:
        con.close()


def convert_db_to_fast_npz(
    db_path: str,
    out_npz: str,
    symbols_file: str = "",
    include_optional: bool = False,
    table: str = "price_indicators",
    chunksize: int = 250_000,
) -> None:
    """
    Streaming conversion from price_indicators to concatenated fast NPZ.

    Required columns:
      symbol, datetime_utc, open, high, low, close
    Optional:
      volume
    """
    check = sqlite_header_check(db_path)
    if not check.get("ok"):
        raise SystemExit(
            "DB looks truncated by header check:\n"
            + json.dumps(check, indent=2)
            + "\nRe-upload/copy the complete DB before conversion."
        )

    qc = sqlite_quick_check(db_path)
    if qc.lower() != "ok":
        raise SystemExit(f"DB quick_check failed: {qc}")

    cols = db_columns(db_path, table)
    required = ["symbol", "datetime_utc", "open", "high", "low", "close"]
    missing = [c for c in required if c not in cols]
    if missing:
        raise SystemExit(f"Missing DB columns: {missing}. Existing: {cols[:50]}")

    select_cols = required[:]
    if "volume" in cols:
        select_cols.append("volume")
    elif "quote_volume" in cols:
        select_cols.append("quote_volume")

    optional_cols = [
        "trend_ma", "trend_ma_prev", "trend_slope_pct",
        "trend_target_pct_long", "trend_target_pct_short",
        "atr_ratio", "dp6h", "dp12h", "quote_volume", "qv_24h",
    ]
    if include_optional:
        for c in optional_cols:
            if c in cols and c not in select_cols:
                select_cols.append(c)

    syms = []
    if symbols_file:
        syms = [
            x.strip()
            for x in Path(symbols_file).read_text(encoding="utf-8").splitlines()
            if x.strip() and not x.strip().startswith("#")
        ]

    where = ""
    params = []
    if syms:
        where = " WHERE symbol IN (%s)" % ",".join(["?"] * len(syms))
        params = syms

    q = f"SELECT {', '.join(select_cols)} FROM {table}{where} ORDER BY symbol ASC, datetime_utc ASC"

    con = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)

    symbols = []
    offsets = [0]
    parts: dict[str, list[np.ndarray]] = {
        "timestamp_s": [], "open": [], "high": [], "low": [], "close": [], "volume": []
    }
    extras: dict[str, list[np.ndarray]] = {}
    cur_sym = None
    cur_chunks = []
    pos = 0

    def flush_symbol(sym: Optional[str], chunks: list[pd.DataFrame]):
        nonlocal pos
        if sym is None or not chunks:
            return
        part = pd.concat(chunks, ignore_index=True)
        part = part.sort_values("datetime_utc")
        n = len(part)
        if n <= 0:
            return
        symbols.append(sym)
        ts = pd.to_datetime(part["datetime_utc"], utc=True, errors="coerce").astype("int64").to_numpy() // 1_000_000_000
        parts["timestamp_s"].append(ts.astype(np.int64))
        for c in ["open", "high", "low", "close"]:
            parts[c].append(pd.to_numeric(part[c], errors="coerce").to_numpy(dtype=np.float64))

        if "volume" in part.columns:
            vol = pd.to_numeric(part["volume"], errors="coerce").to_numpy(dtype=np.float64)
        elif "quote_volume" in part.columns:
            qv = pd.to_numeric(part["quote_volume"], errors="coerce").to_numpy(dtype=np.float64)
            close = np.maximum(pd.to_numeric(part["close"], errors="coerce").to_numpy(dtype=np.float64), 1e-12)
            vol = qv / close
        else:
            vol = np.zeros(n, dtype=np.float64)
        parts["volume"].append(vol)

        for c in part.columns:
            if c in {"symbol", "datetime_utc", "open", "high", "low", "close", "volume"}:
                continue
            try:
                extras.setdefault(c, []).append(pd.to_numeric(part[c], errors="coerce").to_numpy(dtype=np.float64))
            except Exception:
                pass

        pos += n
        offsets.append(pos)
        if len(symbols) % 50 == 0:
            print(f"[convert] symbols={len(symbols)} rows={pos}", flush=True)

    try:
        for chunk in pd.read_sql_query(q, con, params=params, chunksize=chunksize):
            for sym, g in chunk.groupby("symbol", sort=False):
                sym = str(sym)
                if cur_sym is None:
                    cur_sym = sym
                if sym != cur_sym:
                    flush_symbol(cur_sym, cur_chunks)
                    cur_sym = sym
                    cur_chunks = []
                cur_chunks.append(g)
        flush_symbol(cur_sym, cur_chunks)
    finally:
        con.close()

    out = {
        "symbols": np.asarray(symbols, dtype=object),
        "offsets": np.asarray(offsets, dtype=np.int64),
    }
    for c, arrs in parts.items():
        out[c] = np.concatenate(arrs).astype(np.int64 if c == "timestamp_s" else np.float64)
    for c, arrs in extras.items():
        if len(arrs) == len(symbols):
            out[c] = np.concatenate(arrs).astype(np.float64)

    Path(out_npz).parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out_npz, **out)
    print(f"[ok] wrote {out_npz} symbols={len(symbols)} rows={pos}", flush=True)


def slice_data_by_time(data: Dict[str, pd.DataFrame], start: pd.Timestamp, end: pd.Timestamp, min_bars: int):
    out = {}
    for sym, df in data.items():
        m = (df["datetime_utc"] >= start) & (df["datetime_utc"] < end)
        part = df.loc[m].copy()
        if len(part) >= min_bars:
            part = part.reset_index(drop=True)
            part["ret"] = part["close"].pct_change()
            out[sym] = part
    return out


def month_windows_from_data(data: Dict[str, pd.DataFrame], min_bars: int):
    all_times = []
    for df in data.values():
        if len(df):
            all_times.append(df["datetime_utc"].iloc[0])
            all_times.append(df["datetime_utc"].iloc[-1])
    if not all_times:
        return []

    t0 = min(all_times).floor("D")
    t1 = max(all_times).ceil("D")
    starts = pd.date_range(t0.replace(day=1), t1, freq="MS", tz="UTC")
    windows = []
    for s in starts:
        e = s + pd.offsets.MonthBegin(1)
        if e <= t0 or s >= t1:
            continue
        windows.append((max(s, t0), min(e, t1), s.strftime("%Y-%m")))
    return windows


def rolling_windows_from_data(data: Dict[str, pd.DataFrame], days: int, step_days: int):
    all_times = []
    for df in data.values():
        if len(df):
            all_times.append(df["datetime_utc"].iloc[0])
            all_times.append(df["datetime_utc"].iloc[-1])
    if not all_times:
        return []
    t0 = min(all_times).floor("D")
    t1 = max(all_times).ceil("D")
    windows = []
    s = t0
    while s + pd.Timedelta(days=days) <= t1:
        e = s + pd.Timedelta(days=days)
        windows.append((s, e, f"{s.date()}_{e.date()}"))
        s = s + pd.Timedelta(days=step_days)
    return windows


def aggregate_summary(detail: pd.DataFrame) -> pd.DataFrame:
    if detail.empty:
        return detail

    sort_cols = ["period_start", "rank"]
    d = detail.sort_values(sort_cols).copy()
    latest_period = d["period"].iloc[-1] if len(d) else None

    agg = d.groupby("symbol").agg(
        months_tested=("period", "nunique"),
        appearances_top=("rank", "count"),
        best_rank=("rank", "min"),
        avg_rank=("rank", "mean"),
        avg_final_score=("final_phase_short_score", "mean"),
        median_final_score=("final_phase_short_score", "median"),
        avg_proxy_return_total_pct=("proxy_return_total_pct", "mean"),
        median_proxy_return_total_pct=("proxy_return_total_pct", "median"),
        positive_months=("proxy_return_total_pct", lambda x: int((x > 0).sum())),
        median_proxy_mdd_mtm_pct=("proxy_mdd_mtm_pct", "median"),
        worst_proxy_mdd_mtm_pct=("proxy_mdd_mtm_pct", "min"),
        avg_fall_phase_score=("fall_phase_score", "mean"),
        avg_rel_to_btc_pct=("rel_to_btc_pct", "mean"),
        avg_short_setup_success=("short_setup_success_ratio", "mean"),
        avg_pump_fail_ratio=("pump_fail_ratio", "mean"),
    ).reset_index()

    agg["positive_rate"] = agg["positive_months"] / agg["months_tested"].clip(lower=1)

    latest = (
        d.sort_values("period_start")
        .groupby("symbol")
        .tail(1)[[
            "symbol", "period", "rank", "final_phase_short_score", "proxy_return_total_pct",
            "proxy_mdd_mtm_pct", "fall_phase_score", "dd_from_window_high_pct",
            "fall_phase_pos", "recent_ret_pct", "too_late_penalty",
        ]]
        .rename(columns={
            "period": "latest_period",
            "rank": "latest_rank",
            "final_phase_short_score": "latest_final_score",
            "proxy_return_total_pct": "latest_proxy_return_total_pct",
            "proxy_mdd_mtm_pct": "latest_proxy_mdd_mtm_pct",
            "fall_phase_score": "latest_fall_phase_score",
            "dd_from_window_high_pct": "latest_dd_from_high_pct",
            "fall_phase_pos": "latest_fall_phase_pos",
            "recent_ret_pct": "latest_recent_ret_pct",
            "too_late_penalty": "latest_too_late_penalty",
        })
    )

    out = agg.merge(latest, on="symbol", how="left")
    out["portfolio_score"] = (
        1.50 * out["positive_rate"]
        + 0.80 * out["median_proxy_return_total_pct"].rank(pct=True)
        + 0.65 * out["avg_final_score"].rank(pct=True)
        + 0.45 * out["avg_fall_phase_score"].rank(pct=True)
        + 0.45 * out["median_proxy_mdd_mtm_pct"].rank(pct=True)
        - 0.55 * out["latest_too_late_penalty"].fillna(0)
    )
    return out.sort_values(["portfolio_score", "positive_rate", "median_proxy_return_total_pct"], ascending=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="")
    ap.add_argument("--npz", default="")
    ap.add_argument("--converted-npz", default="")
    ap.add_argument("--cfg", default="")
    ap.add_argument("--ranker-path", default="")
    ap.add_argument("--symbols-file", default="")
    ap.add_argument("--include-optional", action="store_true")

    ap.add_argument("--mode", choices=["calendar", "rolling"], default="calendar")
    ap.add_argument("--rolling-days", type=int, default=30)
    ap.add_argument("--rolling-step-days", type=int, default=7)

    ap.add_argument("--min-bars", type=int, default=300)
    ap.add_argument("--top-per-period", type=int, default=100)
    ap.add_argument("--detail-out", default="_reports/akela_monthly_detail.csv")
    ap.add_argument("--summary-out", default="_reports/akela_monthly_summary.csv")
    ap.add_argument("--json-out", default="")

    # pass-through indicator/backtest params
    ap.add_argument("--pump-window", type=int, default=240)
    ap.add_argument("--pump-fail-window", type=int, default=80)
    ap.add_argument("--pump-pct", type=float, default=0.04)
    ap.add_argument("--pump-giveback-frac", type=float, default=0.50)
    ap.add_argument("--setup-up-window", type=int, default=80)
    ap.add_argument("--setup-down-window", type=int, default=80)
    ap.add_argument("--setup-min-up-pct", type=float, default=0.012)
    ap.add_argument("--setup-min-down-pct", type=float, default=0.010)
    ap.add_argument("--phase-recent-window", type=int, default=320)
    ap.add_argument("--late-dd-pct", type=float, default=55.0)
    ap.add_argument("--no-proxy-bt", action="store_true")

    args = ap.parse_args()
    ranker = import_ranker(args.ranker_path)

    npz_path = args.npz
    if args.db:
        out_npz = args.converted_npz or str(Path(args.db).with_suffix(".fast_ohlcv.npz"))
        convert_db_to_fast_npz(
            db_path=args.db,
            out_npz=out_npz,
            symbols_file=args.symbols_file,
            include_optional=args.include_optional,
        )
        npz_path = out_npz

    if not npz_path:
        raise SystemExit("Pass --db or --npz")

    cfg = ranker.load_yaml(args.cfg) if args.cfg else {}
    data = ranker.load_fast_cache(npz_path, min_bars=args.min_bars)
    print(f"[data] loaded symbols={len(data)} from {npz_path}")

    if args.mode == "rolling":
        windows = rolling_windows_from_data(data, args.rolling_days, args.rolling_step_days)
    else:
        windows = month_windows_from_data(data, args.min_bars)

    if not windows:
        raise SystemExit("No windows found.")

    print(f"[windows] {len(windows)}")

    detail_parts = []
    for start, end, label in windows:
        sliced = slice_data_by_time(data, start, end, args.min_bars)
        if len(sliced) < 3:
            print(f"[skip] {label}: symbols={len(sliced)}")
            continue

        # args object compatible with ranker.compute_all
        ranked = ranker.compute_all(sliced, cfg, args).copy()
        if ranked.empty:
            print(f"[skip] {label}: empty rank")
            continue

        ranked = ranked[ranked["is_btc_eth"] == False].copy() if "is_btc_eth" in ranked.columns else ranked
        ranked = ranked.sort_values("final_phase_short_score", ascending=False).head(args.top_per_period)
        ranked.insert(0, "rank", range(1, len(ranked) + 1))
        ranked.insert(0, "period_end", str(end))
        ranked.insert(0, "period_start", str(start))
        ranked.insert(0, "period", label)
        detail_parts.append(ranked)
        print(f"[ok] {label}: symbols={len(sliced)} top={len(ranked)}")

    if not detail_parts:
        raise SystemExit("No monthly/rolling results generated.")

    detail = pd.concat(detail_parts, ignore_index=True)
    summary = aggregate_summary(detail)

    Path(args.detail_out).parent.mkdir(parents=True, exist_ok=True)
    detail.to_csv(args.detail_out, index=False)
    summary.to_csv(args.summary_out, index=False)

    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        summary.head(args.top_per_period).to_json(args.json_out, orient="records", force_ascii=False, indent=2)

    print("\n[SUMMARY TOP]")
    show_cols = [
        "symbol", "portfolio_score", "months_tested", "positive_rate",
        "median_proxy_return_total_pct", "median_proxy_mdd_mtm_pct",
        "avg_final_score", "latest_period", "latest_rank",
        "latest_dd_from_high_pct", "latest_fall_phase_pos", "latest_too_late_penalty",
    ]
    show_cols = [c for c in show_cols if c in summary.columns]
    print(summary[show_cols].head(40).to_string(index=False))
    print(f"\n[detail] {args.detail_out}")
    print(f"[summary] {args.summary_out}")
    if args.json_out:
        print(f"[json] {args.json_out}")


if __name__ == "__main__":
    main()
