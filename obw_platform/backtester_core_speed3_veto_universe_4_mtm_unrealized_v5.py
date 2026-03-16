# -*- coding: utf-8 -*-
"""
backtester_core_speed3_veto_universe_4_mtm_unrealized_fast_v2.py

FAST backtester variant focused on:
- Removing heavy pandas/numpy imports (they dominated startup time in profiling)
- Selecting only the DB columns actually needed by the strategy (including cached fib columns)
- Passing full row dicts (so cached columns are visible to strategy)
- Optional IO skipping for tuning: --no-reports and/or --no-series

Compatible with existing configs, e.g.
  python3 backtester_core_speed3_veto_universe_4_mtm_unrealized_fast_v2.py --cfg configs/final_best_2_mg.yaml --limit-bars 36864
"""

from __future__ import annotations

import argparse
import csv
import importlib
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml


# -------------------------- helpers --------------------------

def import_by_path(path: str):
    mod_name, cls_name = path.rsplit(".", 1)
    mod = importlib.import_module(mod_name)
    return getattr(mod, cls_name)


def find_db_file(path: str) -> str:
    """Find DB file by trying:
    - as-is
    - relative to CWD
    - relative to this file's dir
    - inside ./DB or ../DB
    """
    p = Path(path)
    if p.exists():
        return str(p)

    # as relative to cwd
    p2 = Path(os.getcwd()) / path
    if p2.exists():
        return str(p2)

    # relative to this script location
    here = Path(__file__).resolve().parent
    p3 = here / path
    if p3.exists():
        return str(p3)

    # common DB dirs
    for base in (here / "DB", here.parent / "DB", here / "DB2", here.parent / "DB2"):
        p4 = base / path
        if p4.exists():
            return str(p4)

    raise FileNotFoundError(path)


@dataclass
class Position:
    side: str         # "LONG" / "SHORT"
    entry: float
    sl: float
    tp: float
    qty: float


# -------------------------- pnl helpers --------------------------

def _open_notional(positions: Dict[str, Position]) -> float:
    s = 0.0
    for p in positions.values():
        s += p.entry * p.qty
    return s


def _compute_unrealized(positions: Dict[str, Position], px_map: Dict[str, float], fee: float, slippage: float) -> float:
    """Unrealized PnL (net of 2-sided fee+slippage, same accounting as realized)."""
    u = 0.0
    for sym, pos in positions.items():
        px = px_map.get(sym)
        if px is None:
            continue
        notional = pos.entry * pos.qty
        gross_ret = (px - pos.entry) / pos.entry if pos.side == "LONG" else (pos.entry - px) / pos.entry
        net_ret = gross_ret - 2 * slippage - 2 * fee
        u += net_ret * notional
    return u


# -------------------------- main --------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cfg", required=True, help="YAML config path")
    ap.add_argument("--limit-bars", type=int, default=0, help="Limit number of bars processed")
    ap.add_argument("--plots", default="", help="(kept for compatibility) output dir for plots (optional)")
    ap.add_argument("--no-reports", action="store_true", help="Skip saving bt_trades/bt_summary (faster for tuning)")
    ap.add_argument("--no-series", action="store_true", help="Skip collecting per-bar series arrays (faster)")
    args = ap.parse_args()

    cfg_path = Path(args.cfg)
    if not cfg_path.exists():
        raise FileNotFoundError(str(cfg_path))
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    cache_db = cfg.get("cache_db", "combined_cache_1m_1y.db")
    db_file = find_db_file(cache_db)

    allow_syms = set(cfg.get("allow_symbols", []) or [])
    deny_syms  = set(cfg.get("deny_symbols", []) or [])

    # Load strategy early so we can ask it which DB columns it needs (for cached fib speedups)
    Strat = import_by_path(cfg["strategy_class"])
    required_cols: List[str] = []
    if hasattr(Strat, "required_db_columns"):
        try:
            required_cols = list(Strat.required_db_columns(cfg) or [])
        except Exception:
            required_cols = []

    # Always need these
    base_cols = ["symbol", "datetime_utc", "close"]
    # include OHLC for strategies that might fall back to runtime ATR/pmin logic
    for c in ("open", "high", "low"):
        if c not in base_cols:
            base_cols.append(c)

    # merge + preserve order
    seen = set(base_cols)
    cols = base_cols[:]
    for c in required_cols:
        if c and c not in seen:
            cols.append(c)
            seen.add(c)

    cols_sql = ", ".join(cols)

    con = sqlite3.connect(db_file)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # Determine time range for select. We keep same behavior as original:
    # - If limit-bars > 0: fetch last <limit> distinct datetimes.
    # - Else: fetch all.
    limit_bars = int(args.limit_bars or 0)
    if limit_bars > 0:
        th_row = cur.execute(
            "SELECT datetime_utc FROM price_indicators "
            "WHERE symbol LIKE ? "
            "ORDER BY datetime_utc DESC LIMIT 1 OFFSET ?",
            ("%/%", max(0, limit_bars - 1)),
        ).fetchone()
        if th_row:
            threshold = th_row["datetime_utc"]
            rows = cur.execute(
                f"SELECT {cols_sql} FROM price_indicators "
                "WHERE datetime_utc >= ? "
                "AND symbol LIKE ? "
                "ORDER BY datetime_utc ASC",
                (threshold, "%/%"),
            ).fetchall()
        else:
            rows = []
    else:
        rows = cur.execute(
            f"SELECT {cols_sql} FROM price_indicators "
            "WHERE symbol LIKE ? "
            "ORDER BY datetime_utc ASC",
            ("%/%",),
        ).fetchall()

    if not rows:
        raise RuntimeError("No rows fetched from DB (check symbol format and dates).")

    # Convert sqlite Rows to plain dicts ONCE (strategy uses .get())
    # Also apply allow/deny filters early to reduce work.
    rows2: List[Dict[str, Any]] = []
    if allow_syms or deny_syms:
        for r in rows:
            sym = r["symbol"]
            if allow_syms and (sym not in allow_syms):
                continue
            if sym in deny_syms:
                continue
            rows2.append({k: r[k] for k in cols})
    else:
        rows2 = [{k: r[k] for k in cols} for r in rows]

    # Bucket by time
    slices: List[Tuple[str, List[Dict[str, Any]]]] = []
    cur_t = None
    bucket: List[Dict[str, Any]] = []
    for r in rows2:
        t = r["datetime_utc"]
        if cur_t is None:
            cur_t = t
        if t != cur_t:
            slices.append((cur_t, bucket))
            bucket = []
            cur_t = t
        bucket.append(r)
    if bucket:
        slices.append((cur_t, bucket))

    time_start = slices[0][0]
    time_end = slices[-1][0]
    print(f"[time range] {time_start} -> {time_end}")

    strat = Strat(cfg)

    portfolio = cfg.get("portfolio", {}) or {}
    initial_equity = float(portfolio.get("initial_equity", 100.0))
    pos_notional   = float(portfolio.get("position_notional", 20.0))
    fee      = float(portfolio.get("fee_rate", 0.001))
    slippage = float(portfolio.get("slippage_per_side", 0.0003))
    max_notional_frac = float(portfolio.get("max_notional_frac", 0.5))

    equity_realized = initial_equity  # realized-only
    positions: Dict[str, Position] = {}
    pos_time: Dict[str, str] = {}

    wins = losses = trades = 0
    pnl_pos = 0.0
    pnl_neg = 0.0
    fees_cum = 0.0

    tr_rows: List[Dict[str, Any]] = []

    # Series for plots/diagnostics
    collect_series = not args.no_series
    ts_list: List[str] = []
    eq_real_list: List[float] = []
    eq_mtm_list: List[float] = []
    unreal_list: List[float] = []
    margin_call_excess_list: List[float] = []
    sub_pnl_cum_list: List[float] = []
    sub_pnl_cum = 0.0

    eq_curve_vals = [initial_equity]

    t0 = time.time()

    for t, bucket_rows in slices:
        # Build quick maps
        md_map_all: Dict[str, Dict[str, Any]] = {}
        px_map: Dict[str, float] = {}
        for r in bucket_rows:
            sym = r["symbol"]
            md_map_all[sym] = r
            px_map[sym] = float(r.get("close") or 0.0)

        # --- Exits / partial exits ---
        if positions:
            for sym, pos in list(positions.items()):
                row = md_map_all.get(sym)
                if not row:
                    continue

                # provide equity_mtm context (strategy uses it for km calc)
                unreal_now = _compute_unrealized(positions, px_map, fee, slippage)
                equity_mtm_now = equity_realized + unreal_now
                ctx = {"equity_mtm": float(equity_mtm_now)}

                ex = strat.manage_position(sym, row, pos, ctx=ctx)
                if ex and ex.action in ("TP", "SL", "EXIT"):
                    px = float(ex.exit_price if getattr(ex, "exit_price", None) is not None else row.get("close"))
                    notional = pos.entry * pos.qty
                    gross_ret = (px - pos.entry) / pos.entry if pos.side == "LONG" else (pos.entry - px) / pos.entry
                    net_ret = gross_ret - 2 * slippage - 2 * fee
                    pnl_amt = net_ret * notional

                    trades += 1
                    fees_cum += fee * 2 * notional
                    if pnl_amt > 0:
                        wins += 1
                        pnl_pos += pnl_amt
                    else:
                        losses += 1
                        pnl_neg += pnl_amt
                    equity_realized += pnl_amt

                    if not args.no_reports:
                        tr_rows.append({
                            "symbol": sym, "side": pos.side,
                            "entry_time": pos_time.get(sym, t), "exit_time": t,
                            "entry": pos.entry, "exit": px,
                            "tp": pos.tp, "sl": pos.sl,
                            "action": ex.action, "reason": getattr(ex, "reason", None) or ex.action,
                            "gross_return": gross_ret, "net_return": net_ret,
                            "notional": notional,
                            "fees_paid": fee * 2 * notional,
                            "realized_pnl": pnl_amt,
                            "unrealized_pnl": 0.0,
                            "sub_trade_pnl": 0.0,
                        })

                    del positions[sym]
                    pos_time.pop(sym, None)

                elif ex and ex.action == "TP_PARTIAL":
                    px = float(ex.exit_price if getattr(ex, "exit_price", None) is not None else row.get("close"))
                    part = max(0.0, min(1.0, float(getattr(ex, "qty_frac", 0.5) or 0.5)))
                    qty_close = pos.qty * part
                    notional_now = qty_close * px

                    min_notional = float(getattr(strat, "exchange_min_notional", 0.0) or 0.0)
                    min_qty = float(getattr(strat, "min_qty", 0.0) or 0.0)
                    if notional_now >= min_notional and (min_qty <= 0 or qty_close >= min_qty):
                        notional_entry = qty_close * pos.entry
                        gross_ret = (px - pos.entry) / pos.entry if pos.side == "LONG" else (pos.entry - px) / pos.entry
                        net_ret = gross_ret - 2 * slippage - 2 * fee
                        pnl_amt = net_ret * notional_entry

                        trades += 1
                        fees_cum += fee * 2 * notional_entry
                        if pnl_amt > 0:
                            wins += 1
                            pnl_pos += pnl_amt
                        else:
                            losses += 1
                            pnl_neg += pnl_amt
                        equity_realized += pnl_amt

                        sub_pnl_cum += pnl_amt

                        if not args.no_reports:
                            tr_rows.append({
                                "symbol": sym, "side": pos.side,
                                "entry_time": pos_time.get(sym, t), "exit_time": t,
                                "entry": pos.entry, "exit": px,
                                "tp": pos.tp, "sl": pos.sl,
                                "action": "TP_PARTIAL", "reason": getattr(ex, "reason", "TP_PARTIAL"),
                                "gross_return": gross_ret, "net_return": net_ret,
                                "notional": notional_entry,
                                "fees_paid": fee * 2 * notional_entry,
                                "realized_pnl": pnl_amt,
                                "unrealized_pnl": 0.0,
                                "sub_trade_pnl": pnl_amt,
                            })

                        pos.qty -= qty_close

        # --- diagnostics after exits ---
        unrealized = _compute_unrealized(positions, px_map, fee, slippage)
        equity_mtm = equity_realized + unrealized

        open_notional = _open_notional(positions)
        equity_cap = max(0.0, equity_mtm)
        allowed_notional = max_notional_frac * equity_cap
        margin_call_excess = max(0.0, open_notional - allowed_notional)

        if collect_series:
            ts_list.append(str(t))
            eq_real_list.append(float(equity_realized))
            eq_mtm_list.append(float(equity_mtm))
            unreal_list.append(float(unrealized))
            margin_call_excess_list.append(float(margin_call_excess))
            sub_pnl_cum_list.append(float(sub_pnl_cum))

        # --- Strategy-owned candidate selection ---
        md_map_open = md_map_all
        universe_syms = strat.universe(t, md_map_open)
        ranked_syms = strat.rank(t, md_map_open, universe_syms)

        # --- OPEN entries ---
        for sym in ranked_syms:
            if sym in positions:
                continue
            current_open = _open_notional(positions)
            if (current_open + pos_notional) > max_notional_frac * equity_mtm:
                break

            row = md_map_open.get(sym)
            if not row:
                continue

            ctx = {"equity_mtm": float(equity_mtm)}
            sig = strat.entry_signal(True, sym, row, ctx=ctx)
            if sig is None:
                continue
            if sig.side not in ("LONG", "SHORT"):
                raise RuntimeError(f"Strategy must supply side LONG/SHORT for {sym}")

            tp = getattr(sig, "take_profit", getattr(sig, "tp_price", getattr(sig, "tp", None)))
            sl = getattr(sig, "stop_price", getattr(sig, "sl_price", getattr(sig, "sl", None)))

            # Allow strategies to omit SL (e.g. live trading with no protective stop order).
            # In that case we assign an ultra-wide "fallback SL" so the backtester can run.
            if not isinstance(tp, (int, float)):
                raise RuntimeError(f"Strategy must supply numeric take_profit for {sym}")

            if not isinstance(sl, (int, float)):
                allow_no_sl = bool(portfolio.get("allow_no_sl", True))
                if not allow_no_sl:
                    raise RuntimeError(f"Strategy must supply numeric stop_price for {sym}")
                sl_fallback_pct = float(portfolio.get("sl_fallback_pct", 99.99))  # percent distance from entry
                entry_px_tmp = float(row.get("close") or 0.0)
                if sig.side == "LONG":
                    sl = max(1e-12, entry_px_tmp * (1.0 - sl_fallback_pct / 100.0))
                else:  # SHORT
                    sl = max(1e-12, entry_px_tmp * (1.0 + sl_fallback_pct / 100.0))

            entry_px = float(row.get("close") or 0.0)
            qty = pos_notional / max(entry_px, 1e-12)
            positions[sym] = Position(sig.side, entry_px, float(sl), float(tp), qty)
            pos_time[sym] = t

        eq_curve_vals.append(float(equity_mtm))

    elapsed_sec = time.time() - t0

    # --- Summary ---
    equity_end_realized = float(equity_realized)
    equity_end_mtm = float(eq_curve_vals[-1] if eq_curve_vals else equity_realized)
    pf = (pnl_pos / abs(pnl_neg)) if pnl_neg < 0 else 0.0
    win_rate = 100.0 * wins / max(1, trades)
    # simple max dd on mtm curve
    peak = -1e18
    max_dd = 0.0
    for v in eq_curve_vals:
        if v > peak:
            peak = v
        dd = (v - peak) / max(peak, 1e-9)
        if dd < max_dd:
            max_dd = dd

    # monotonicity proxy
    mono = 0.0
    if len(eq_curve_vals) > 2:
        pos_d = 0
        for i in range(1, len(eq_curve_vals)):
            if eq_curve_vals[i] >= eq_curve_vals[i - 1]:
                pos_d += 1
        mono = pos_d / (len(eq_curve_vals) - 1) - 1.0

    days = max(1e-9, len(slices) / 1440.0)
    daily_ret = (equity_end_realized / initial_equity) ** (1.0 / days) - 1.0 if initial_equity > 0 else 0.0
    monthly_ret = (1.0 + daily_ret) ** 30 - 1.0
    yearly_ret = (1.0 + daily_ret) ** 365 - 1.0
    apr = yearly_ret * 100.0

    summary = {
        "equity_end_realized": equity_end_realized,
        "equity_end_mtm": equity_end_mtm,
        "trades": trades,
        "pf": pf,
        "fees_realized": fees_cum,
        "win_rate": win_rate,
        "max_dd_mtm": max_dd * 100.0,
        "mono_mtm": mono * 100.0,
        "elapsed_sec": elapsed_sec,
        "apr_realized": apr,
        "daily_ret_realized": daily_ret * 100.0,
        "monthly_ret_realized": monthly_ret * 100.0,
        "yearly_ret_realized": yearly_ret * 100.0,
        "sub_pnl_total": sub_pnl_cum,
        "margin_call_excess_max": max(margin_call_excess_list) if margin_call_excess_list else 0.0,
    }

    if not args.no_reports:
        report_dir = Path(cfg.get("report_dir", "_reports/_backtest"))
        report_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
        run_dir = report_dir / f"backtest_{cfg_path.stem}_{ts}"
        run_dir.mkdir(parents=True, exist_ok=True)

        bt_trades_path = run_dir / "bt_trades.csv"
        bt_summary_path = run_dir / "bt_summary.csv"

        if tr_rows:
            with open(bt_trades_path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(tr_rows[0].keys()))
                w.writeheader()
                w.writerows(tr_rows)
        else:
            with open(bt_trades_path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["symbol", "side", "entry_time", "exit_time", "entry", "exit", "tp", "sl", "action",
                            "reason", "gross_return", "net_return", "notional", "fees_paid", "realized_pnl",
                            "unrealized_pnl", "sub_trade_pnl"])

        with open(bt_summary_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(summary.keys()))
            w.writeheader()
            w.writerow(summary)

        print(f"[files] bt_trades={bt_trades_path} bt_summary={bt_summary_path}")
        print(f"[reports] saved to {run_dir}")

    print(
        f"equity_end_realized={summary['equity_end_realized']:.6f} "
        f"equity_end_mtm={summary['equity_end_mtm']:.6f} "
        f"trades={summary['trades']} pf={summary['pf']:.6f} "
        f"fees_realized={summary['fees_realized']:.6f} win_rate={summary['win_rate']:.3f}% "
        f"max_dd_mtm={summary['max_dd_mtm']:.3f}% mono_mtm={summary['mono_mtm']:.3f}% "
        f"elapsed_sec={summary['elapsed_sec']:.6f} apr_realized={summary['apr_realized']:.3f}% "
        f"daily_ret_realized={summary['daily_ret_realized']:.3f}% monthly_ret_realized={summary['monthly_ret_realized']:.3f}% "
        f"yearly_ret_realized={summary['yearly_ret_realized']:.3f}% sub_pnl_total={summary['sub_pnl_total']:.6f} "
        f"margin_call_excess_max={summary['margin_call_excess_max']:.6f}"
    )


if __name__ == "__main__":
    main()
