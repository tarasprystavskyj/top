#!/usr/bin/env python3
# backtester_core_speed3_veto_universe_2_mtm_unrealized.py
# Thin backtester (universe + strategy-owned logic) + richer PnL accounting/plots
#
# Changes vs backtester_core_speed3_veto_universe_2.py:
# 1) Track realized PnL and unrealized PnL separately.
#    - equity_realized updates ONLY on realized exits (TP/SL/EXIT/TP_PARTIAL).
#    - At EOD we DO NOT realize unrealized PnL into equity_realized.
# 2) Generate additional plots:
#    - unrealized PnL over time
#    - total PnL (realized + unrealized) over time
#    - margin-call stress (excess open exposure over allowed cap) over time
#    - cumulative sub-sell PnL (from TP_PARTIAL)
#    - all plots combined into one image
#
# Notes:
# - "Margin call" here is a diagnostic metric:
#     margin_call_excess = max(0, open_notional - max_notional_frac * equity_mtm)
#   It indicates how much exposure would need to be reduced to respect the cap.
# - "Sub-sell PnL" is realized_pnl on TP_PARTIAL rows (and/or reason contains 'Sub-sell').

import argparse, sqlite3, importlib, time, sys, os, pathlib as _p, shutil, json
from dataclasses import dataclass
from typing import Dict, Any, List, Optional

import yaml
import pandas as pd
import datetime as _dt

def import_by_path(path: str):
    mod_name, cls_name = path.rsplit(".", 1)
    root = str((_p.Path(__file__).parent).resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    mod = importlib.import_module(mod_name)
    return getattr(mod, cls_name)

def _db_connect(path: str):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con

@dataclass
class Position:
    side: str
    entry: float
    sl: float
    tp: float
    qty: float

def _split_csv_list(s):
    if not s: return []
    return [x.strip() for x in str(s).split(",") if x.strip()]

def find_db_file(filename: str):
    filename = filename.strip()
    if os.path.isabs(filename) and os.path.exists(filename):
        return os.path.abspath(filename)
    if os.path.dirname(filename) and os.path.exists(filename):
        return os.path.abspath(filename)
    search_paths = [
        os.path.join(".", filename),
        os.path.join("..", filename),
        os.path.join("..", "DB", filename),
    ]
    for path in search_paths:
        if os.path.exists(path):
            return os.path.abspath(path)
    raise FileNotFoundError(f"DB file '{filename}' not found in {search_paths}")

def _timeframe_to_minutes(tf: str) -> float:
    s = str(tf).strip().lower()
    if s.endswith("m"):
        try: return float(s[:-1])
        except Exception: return 0.0
    if s.endswith("h"):
        try: return float(s[:-1]) * 60.0
        except Exception: return 0.0
    if s.endswith("d"):
        try: return float(s[:-1]) * 1440.0
        except Exception: return 0.0
    try: return float(s)
    except Exception: return 0.0

def _norm_iso(ts: str):
    if not ts:
        return ts
    try:
        return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).isoformat()
    except Exception:
        return ts

def _compute_unrealized(positions: Dict[str, Position], px_map: Dict[str, float], fee: float, slippage: float) -> float:
    unreal = 0.0
    for sym, pos in positions.items():
        px = px_map.get(sym)
        if px is None:
            continue
        if pos.side == "LONG":
            gross_ret = (px - pos.entry) / max(pos.entry, 1e-12)
        else:
            gross_ret = (pos.entry - px) / max(pos.entry, 1e-12)
        net_ret = gross_ret - 2 * slippage - 2 * fee
        unreal += net_ret * (pos.entry * pos.qty)
    return float(unreal)

def _open_notional(positions: Dict[str, Position]) -> float:
    return float(sum(p.entry * p.qty for p in positions.values()))

def main():
    ap = argparse.ArgumentParser(description="Thin backtester + realized/unrealized PnL accounting")
    ap.add_argument("--cfg", required=True)
    ap.add_argument("--limit-bars", type=int, default=500)
    ap.add_argument("--time-from", dest="time_from", type=str, default=None)
    ap.add_argument("--time-to", dest="time_to", type=str, default=None)
    ap.add_argument("--allow-symbols", type=str, default="")
    ap.add_argument("--plots", dest="plots_dir", type=str, default=None)
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--symbols-file", dest="symbols_file")
    ap.add_argument("--deny-symbols", dest="deny_symbols")
    ap.add_argument("--cache_db", dest="cache_db")
    args = ap.parse_args()

    t0 = time.time()
    cfg = yaml.safe_load(open(args.cfg, "r"))
    cache_db = args.cache_db or cfg.get("cache_db") or cfg.get("cache_db_path")
    if not cache_db:
        raise KeyError("cfg must include cache_db or cache_db_path (or pass --cache_db)")
    db_file = find_db_file(cache_db)
    con = _db_connect(db_file)

    # Allow/Deny sets
    allow_syms, deny_syms = set(), set()
    allow_syms |= set(_split_csv_list(args.allow_symbols))
    deny_syms  |= set(_split_csv_list(args.deny_symbols))

    sym_file = args.symbols_file or cfg.get("symbols_file") or cfg.get("universe_file")
    if sym_file:
        if not os.path.isabs(sym_file):
            udir = os.path.join(os.path.dirname(__file__), "universe")
            sym_file = os.path.join(udir, os.path.basename(sym_file))
        with open(sym_file, "r", encoding="utf-8") as f:
            for ln in f:
                s = ln.strip()
                if s and not s.startswith("#"):
                    allow_syms.add(s)

    allow_syms |= set(cfg.get("universe_include", []) or [])
    deny_syms  |= set(cfg.get("universe_exclude", []) or [])
    if allow_syms: print(f"[universe] allow list size = {len(allow_syms)}")
    if deny_syms:  print(f"[universe] deny  list size = {len(deny_syms)}")

    # Time window
    t_from = _norm_iso(getattr(args, "time_from", None))
    t_to   = _norm_iso(getattr(args, "time_to", None))
    allow = [s.strip() for s in (args.allow_symbols or "").split(",") if s.strip()]

    rows = []
    if t_from or t_to:
        q = [
            "SELECT symbol, datetime_utc, close, atr_ratio, dp6h, dp12h, quote_volume, qv_24h",
            "FROM price_indicators WHERE 1=1",
        ]
        params = []
        if t_from:
            q.append("AND datetime_utc >= ?"); params.append(t_from)
        if t_to:
            q.append("AND datetime_utc <= ?"); params.append(t_to)
        if allow:
            q.append(f"AND symbol IN ({','.join('?'*len(allow))})"); params.extend(allow)
        q.append("ORDER BY datetime_utc ASC, symbol ASC")
        rows = con.execute(" ".join(q), params).fetchall()
        if not rows or not t_from or not t_to:
            print(f"No bars in interval {t_from} .. {t_to} for DB={db_file}"); return
        rr = con.execute(
            "SELECT MIN(datetime_utc), MAX(datetime_utc), COUNT(*) FROM price_indicators WHERE datetime_utc BETWEEN ? AND ?",
            (t_from, t_to),
        ).fetchone()
        time_start, time_end, rows_count = rr[0], rr[1], rr[2]
        if args.debug:
            print(f"[dbg] rows_in_range={rows_count} db_min={time_start} db_max={time_end}")
    else:
        if allow:
            placeholders = ",".join("?" * len(allow))
            th_row = con.execute(
                f"""
                SELECT MIN(datetime_utc) FROM (
                    SELECT DISTINCT datetime_utc FROM price_indicators
                    WHERE symbol IN ({placeholders})
                    ORDER BY datetime_utc DESC LIMIT ?
                )
                """,
                (*allow, int(args.limit_bars)),
            ).fetchone()
        else:
            th_row = con.execute(
                """
                SELECT MIN(datetime_utc) FROM (
                    SELECT DISTINCT datetime_utc FROM price_indicators
                    ORDER BY datetime_utc DESC LIMIT ?
                )
                """,
                (int(args.limit_bars),),
            ).fetchone()
        if not th_row or not th_row[0]:
            print("No bars."); return
        min_time = th_row[0]
        q = [
            "SELECT symbol, datetime_utc, close, atr_ratio, dp6h, dp12h, quote_volume, qv_24h",
            "FROM price_indicators WHERE datetime_utc >= ?",
        ]
        params = [min_time]
        if allow:
            q.append(f"AND symbol IN ({','.join('?'*len(allow))})"); params.extend(allow)
        q.append("ORDER BY datetime_utc ASC, symbol ASC")
        rows = con.execute(" ".join(q), params).fetchall()
        if not rows:
            print("No bars."); return
        time_start = rows[0]["datetime_utc"]
        time_end = rows[-1]["datetime_utc"]
    print(f"[time range] {time_start} -> {time_end}")

    # Bucket by time
    slices = []
    cur_t, bucket = None, []
    for r in rows:
        t = r["datetime_utc"]
        if cur_t is None: cur_t = t
        if t != cur_t:
            slices.append((cur_t, bucket)); bucket = []; cur_t = t
        bucket.append((
            r["symbol"],
            float(r["close"] or 0.0),
            float(r["atr_ratio"] or 0.0),
            float(r["dp6h"] or 0.0),
            float(r["dp12h"] or 0.0),
            float(r["quote_volume"] or 0.0),
            float(r["qv_24h"] or 0.0),
        ))
    if bucket: slices.append((cur_t, bucket))
    bars_count = len(slices)

    Strat = import_by_path(cfg["strategy_class"])
    strat = Strat(cfg)

    portfolio = cfg.get("portfolio", {})
    initial_equity = float(portfolio.get("initial_equity", 100.0))
    pos_notional   = float(portfolio.get("position_notional", 20.0))
    fee      = float(portfolio.get("fee_rate", 0.001))
    slippage = float(portfolio.get("slippage_per_side", 0.0003))
    max_notional_frac = float(portfolio.get("max_notional_frac", 0.5))

    equity_realized = initial_equity  # realized-only equity
    positions: Dict[str, Position] = {}
    pos_time: Dict[str, str] = {}

    wins=losses=trades=0
    pnl_pos=0.0; pnl_neg=0.0; fees_cum=0.0

    tr_rows: List[Dict[str, Any]] = []

    # time-series for diagnostics/plots (one point per bar)
    ts_list: List[str] = []
    eq_real_list: List[float] = []
    eq_mtm_list: List[float] = []
    unreal_list: List[float] = []
    margin_call_excess_list: List[float] = []
    sub_pnl_cum_list: List[float] = []
    sub_pnl_cum = 0.0

    # keep equity curve by bar (mtm) for old metrics too
    eq_curve_vals = [initial_equity]

    for t, bucket_all in slices:
        px_map = {sym: close for (sym, close, *_rest) in bucket_all}

        # --- Exits / partial exits ---
        if positions:
            for sym, pos in list(positions.items()):
                row = None
                for tup in bucket_all:
                    if tup[0] == sym:
                        sym2, close, atr, dp6, dp12, qv1h, qv24 = tup
                        row = {"close": close, "atr_ratio": atr, "dp6h": dp6, "dp12h": dp12, "quote_volume": qv1h, "qv_24h": qv24}
                        break
                if row is None:
                    continue

                ex = strat.manage_position(sym, row, pos, ctx=None)
                if ex and ex.action in ("TP", "SL", "EXIT"):
                    px = float(ex.exit_price if ex.exit_price is not None else row["close"])
                    notional = pos.entry * pos.qty
                    gross_ret = (px - pos.entry) / pos.entry if pos.side == "LONG" else (pos.entry - px) / pos.entry
                    net_ret = gross_ret - 2 * slippage - 2 * fee
                    pnl_amt = net_ret * notional

                    trades += 1
                    fees_cum += fee * 2 * notional
                    if pnl_amt > 0:
                        wins += 1; pnl_pos += pnl_amt
                    else:
                        losses += 1; pnl_neg += pnl_amt
                    equity_realized += pnl_amt

                    tr_rows.append({
                        "symbol": sym, "side": pos.side,
                        "entry_time": pos_time.get(sym, t), "exit_time": t,
                        "entry": pos.entry, "exit": px,
                        "tp": pos.tp, "sl": pos.sl,
                        "action": ex.action, "reason": ex.reason or ex.action,
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
                    px = float(ex.exit_price if ex.exit_price is not None else row["close"])
                    part = max(0.0, min(1.0, float(getattr(ex, "qty_frac", 0.5))))
                    qty_close = pos.qty * part
                    notional_now = qty_close * px

                    # optional exchange min filters (if strategy provides)
                    min_notional = getattr(strat, "exchange_min_notional", 0.0)
                    min_qty = getattr(strat, "min_qty", 0.0)
                    if notional_now >= min_notional and (min_qty <= 0 or qty_close >= min_qty):
                        notional_entry = qty_close * pos.entry
                        gross_ret = (px - pos.entry) / pos.entry if pos.side == "LONG" else (pos.entry - px) / pos.entry
                        net_ret = gross_ret - 2 * slippage - 2 * fee
                        pnl_amt = net_ret * notional_entry

                        trades += 1
                        fees_cum += fee * 2 * notional_entry
                        if pnl_amt > 0:
                            wins += 1; pnl_pos += pnl_amt
                        else:
                            losses += 1; pnl_neg += pnl_amt
                        equity_realized += pnl_amt

                        # sub-trade pnl classification
                        is_sub = ("Sub-sell" in str(getattr(ex, "reason", ""))) or True  # TP_PARTIAL is treated as sub-sell
                        sub_trade_pnl = pnl_amt if is_sub else 0.0
                        if is_sub:
                            sub_pnl_cum += sub_trade_pnl

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
                            "sub_trade_pnl": sub_trade_pnl,
                        })

                        pos.qty -= qty_close

        # --- diagnostics after exits ---
        unrealized = _compute_unrealized(positions, px_map, fee, slippage)
        equity_mtm = equity_realized + unrealized
        open_notional = _open_notional(positions)
        allowed_notional = max_notional_frac * equity_mtm
        margin_call_excess = max(0.0, open_notional - allowed_notional)

        ts_list.append(str(t))
        eq_real_list.append(float(equity_realized))
        eq_mtm_list.append(float(equity_mtm))
        unreal_list.append(float(unrealized))
        margin_call_excess_list.append(float(margin_call_excess))
        sub_pnl_cum_list.append(float(sub_pnl_cum))

        # --- Universe filtering for OPENINGS only (allow/deny) ---
        md_map_all = {
            sym: {"close":close,"atr_ratio":atr,"dp6h":dp6,"dp12h":dp12,"quote_volume":qv1h,"qv_24h":qv24}
            for (sym, close, atr, dp6, dp12, qv1h, qv24) in bucket_all
        }
        if allow_syms or deny_syms:
            md_map_open = {s:r for s,r in md_map_all.items()
                           if ((not allow_syms) or (s in allow_syms)) and (s not in deny_syms)}
            if not md_map_open:
                eq_curve_vals.append(equity_mtm)
                continue
        else:
            md_map_open = md_map_all

        # --- Strategy-owned candidate selection ---
        universe_syms = strat.universe(t, md_map_open)
        ranked_syms   = strat.rank(t, md_map_open, universe_syms)

        # --- OPEN entries via strategy ---
        for sym in ranked_syms:
            if sym in positions:
                continue
            current_open = _open_notional(positions)
            if (current_open + pos_notional) > max_notional_frac * equity_mtm:
                break
            row = md_map_open.get(sym)
            if not row:
                continue
            sig = strat.entry_signal(True, sym, row, ctx=None)
            if sig is None:
                continue
            if sig.side not in ("LONG","SHORT"):
                raise RuntimeError(f"Strategy must supply side LONG/SHORT for {sym}")
            tp = getattr(sig, "take_profit", getattr(sig, "tp_price", getattr(sig, "tp", None)))
            sl = getattr(sig, "stop_price", getattr(sig, "sl_price", getattr(sig, "sl", None)))
            if not isinstance(tp, (int,float)) or not isinstance(sl, (int,float)):
                raise RuntimeError(f"Strategy must supply numeric take_profit/stop_price for {sym}")
            entry_px = float(row["close"])
            qty = pos_notional / max(entry_px, 1e-12)
            positions[sym] = Position(sig.side, entry_px, float(sl), float(tp), qty)
            pos_time[sym] = t

        eq_curve_vals.append(equity_mtm)

    # --- EOD handling: DO NOT realize unrealized PnL into equity_realized ---
    # We still record a synthetic row for visibility, but realized_pnl stays 0.
    if slices and positions:
        last_t = slices[-1][0]
        last_px = {sym: close for (sym, close, *_rest) in slices[-1][1]}
        for sym, pos in list(positions.items()):
            px = last_px.get(sym)
            if px is None:
                continue
            notional = pos.entry * pos.qty
            gross_ret = (px - pos.entry) / pos.entry if pos.side == "LONG" else (pos.entry - px) / pos.entry
            net_ret = gross_ret - 2 * slippage - 2 * fee
            unreal_amt = net_ret * notional

            # record "EOD" row without affecting realized equity
            tr_rows.append({
                "symbol": sym, "side": pos.side,
                "entry_time": pos_time.get(sym, last_t), "exit_time": last_t,
                "entry": pos.entry, "exit": px,
                "tp": pos.tp, "sl": pos.sl,
                "action": "EOD", "reason": "EOD (unrealized only)",
                "gross_return": gross_ret, "net_return": net_ret,
                "notional": notional,
                "fees_paid": 0.0,            # not realized
                "realized_pnl": 0.0,         # IMPORTANT
                "unrealized_pnl": unreal_amt,
                "sub_trade_pnl": 0.0,
            })

            del positions[sym]
            pos_time.pop(sym, None)

        # update last diagnostics point after clearing positions
        ts_list.append(str(last_t))
        eq_real_list.append(float(equity_realized))
        eq_mtm_list.append(float(equity_realized))  # positions cleared, unreal=0
        unreal_list.append(0.0)
        margin_call_excess_list.append(0.0)
        sub_pnl_cum_list.append(float(sub_pnl_cum))

    elapsed = time.time() - t0
    pf = (pnl_pos / max(1e-12, -pnl_neg)) if (pnl_pos>0 and pnl_neg<0) else 0.0
    win_rate_pct = (wins * 100.0 / max(1, trades)) if trades else 0.0

    tf_minutes = _timeframe_to_minutes(cfg.get("timeframe", 0))
    if t_from or t_to:
        try:
            t0_dt = pd.to_datetime(time_start)
            t1_dt = pd.to_datetime(time_end)
            total_minutes = (t1_dt - t0_dt).total_seconds() / 60.0
        except Exception:
            total_minutes = tf_minutes * float(args.limit_bars or 0)
    else:
        total_minutes = tf_minutes * float(args.limit_bars or 0)
    total_days = total_minutes / (60.0 * 24.0) if total_minutes else 0.0

    # Returns based on realized-only equity (per requirement)
    total_return = (equity_realized / initial_equity) if initial_equity else 0.0
    if total_days > 0 and total_return > 0:
        daily_ret = total_return ** (1.0 / total_days) - 1.0
        monthly_ret = total_return ** (1.0 / (total_days / 30.0)) - 1.0 if total_days >= 1 else 0.0
        yearly_ret = total_return ** (1.0 / (total_days / 365.0)) - 1.0 if total_days >= 1 else 0.0
        apr = ((equity_realized - initial_equity) / initial_equity) * (365.0 / total_days)
    else:
        daily_ret = monthly_ret = yearly_ret = apr = 0.0

    # Metrics based on MTM curve (still useful for DD)
    import numpy as _np
    eq_arr = _np.array(eq_curve_vals, dtype=float)
    if eq_arr.size >= 2:
        peaks = _np.maximum.accumulate(eq_arr)
        dd_arr = (eq_arr - peaks) / peaks
        max_dd_frac = float(dd_arr.min())
        deltas = _np.diff(eq_arr)
        up = int((deltas > 0).sum()); down = int((deltas < 0).sum()); steps = max(1, deltas.size)
        mono_sign = float((up - down) / steps)
        total_mov = float(_np.abs(deltas).sum()) + 1e-12
        mono_mag = float((deltas.sum()) / total_mov)
    else:
        max_dd_frac = 0.0; mono_sign = 0.0; mono_mag = 0.0

    summary_dict = {
        "equity_start": initial_equity,
        "equity_end_realized": float(equity_realized),
        "equity_end_mtm": float(eq_mtm_list[-1] if eq_mtm_list else equity_realized),
        "realized_pnl_total": float(equity_realized - initial_equity),
        "unrealized_pnl_last": float(unreal_list[-1] if unreal_list else 0.0),
        "trades": trades,
        "profit_factor": pf,
        "win_rate_%": win_rate_pct,
        "elapsed_sec": elapsed,
        "max_dd_frac": max_dd_frac,
        "max_dd_%": (max_dd_frac * 100.0),
        "monotonicity_sign": mono_sign,
        "monotonicity_mag": mono_mag,
        "total_fees_realized": fees_cum,
        "apr_%": float(apr * 100.0),
        "daily_return_%": float(daily_ret * 100.0),
        "monthly_return_%": float(monthly_ret * 100.0),
        "yearly_return_%": float(yearly_ret * 100.0),
        "sub_trade_pnl_total": float(sub_pnl_cum),
        "margin_call_excess_max": float(max(margin_call_excess_list) if margin_call_excess_list else 0.0),
    }

    cfg_name = os.path.splitext(os.path.basename(args.cfg))[0]
    time_id = time.strftime("%Y%m%d_%H%M%S")
    report_dir = os.path.abspath(os.path.join("_reports", "_backtest", f"backtest_{cfg_name}_{time_id}"))
    os.makedirs(report_dir, exist_ok=True)

    trades_df = pd.DataFrame(tr_rows)
    trades_csv  = os.path.join(report_dir, "trades.csv")
    summary_csv = os.path.join(report_dir, "summary.csv")
    trades_df.to_csv(trades_csv, index=False)
    pd.DataFrame([summary_dict]).to_csv(summary_csv, index=False)

    trades_csv_bt  = os.path.join(report_dir, "bt_trades.csv")
    summary_csv_bt = os.path.join(report_dir, "bt_summary.csv")
    trades_df.to_csv(trades_csv_bt, index=False)
    with open(summary_csv_bt, "w", encoding="utf-8") as f:
        json.dump(summary_dict, f, indent=2, default=str)

    print(f"[files] bt_trades={trades_csv_bt} bt_summary={summary_csv_bt}")

    # Plots
    if args.plots_dir:
        run_plots_dir = args.plots_dir
        os.makedirs(run_plots_dir, exist_ok=True)
        try:
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates

            # Prepare time series as datetime
            ts_raw = pd.Series(ts_list, dtype=str)
            ts = pd.to_datetime(ts_raw, errors="coerce", utc=True)

            # Individual plots
            def _save_simple(y, title, ylabel, fname):
                plt.figure()
                plt.plot(ts, y)
                ax = plt.gca()
                ax.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d'))
                plt.xticks(rotation=45)
                plt.title(title); plt.xlabel("Time"); plt.ylabel(ylabel)
                plt.tight_layout()
                plt.savefig(os.path.join(run_plots_dir, fname), dpi=160)
                plt.close()

            _save_simple(eq_real_list, "Equity (realized only) vs Time", "Equity", "equity_realized_by_time.png")
            _save_simple(unreal_list, "Unrealized PnL vs Time", "Unrealized PnL", "unrealized_pnl_by_time.png")
            _save_simple([a+b for a,b in zip(eq_real_list, unreal_list)], "Equity MTM (real+unreal) vs Time", "Equity MTM", "equity_mtm_by_time.png")
            _save_simple(margin_call_excess_list, "Margin-call excess exposure vs Time", "Excess Notional", "margin_call_excess_by_time.png")
            _save_simple(sub_pnl_cum_list, "Cumulative sub-sell PnL (TP_PARTIAL) vs Time", "Sub-sell PnL (cum)", "subsell_pnl_by_time.png")

            # Combined figure (all in one)
            fig, axes = plt.subplots(6, 1, figsize=(12, 18), sharex=True)

            axes[0].plot(ts, eq_real_list)
            axes[0].set_title("Equity (realized only)")

            axes[1].plot(ts, unreal_list)
            axes[1].set_title("Unrealized PnL (not realized at EOD)")

            total_eq = [a+b for a,b in zip(eq_real_list, unreal_list)]
            axes[2].plot(ts, total_eq)
            axes[2].set_title("Equity MTM = realized + unrealized")

            axes[3].plot(ts, margin_call_excess_list)
            axes[3].set_title("Margin-call excess exposure (diagnostic)")

            axes[4].plot(ts, sub_pnl_cum_list)
            axes[4].set_title("Cumulative sub-sell PnL (TP_PARTIAL)")

            # "Sum" panel: show cumulative realized pnl, unrealized pnl, and their sum
            realized_pnl_cum = [x - initial_equity for x in eq_real_list]
            axes[5].plot(ts, realized_pnl_cum, label="Realized PnL (cum)")
            axes[5].plot(ts, unreal_list, label="Unrealized PnL")
            axes[5].plot(ts, [r+u for r,u in zip(realized_pnl_cum, unreal_list)], label="Total PnL (real+unreal)")
            axes[5].set_title("PnL components")
            axes[5].legend()

            axes[-1].xaxis.set_major_formatter(mdates.DateFormatter('%m-%d'))
            for ax in axes:
                ax.grid(True, alpha=0.2)
            plt.xticks(rotation=45)
            plt.tight_layout()
            fig.savefig(os.path.join(run_plots_dir, "pnl_panels_all.png"), dpi=160)
            plt.close(fig)

        except Exception as e:
            print(f"[plots] failed: {e}")

    # Consolidate plots into report dir
    try:
        if args.plots_dir and os.path.isdir(args.plots_dir):
            dst_plots = os.path.join(report_dir, "plots")
            os.makedirs(dst_plots, exist_ok=True)
            for item in os.listdir(args.plots_dir):
                s = os.path.join(args.plots_dir, item)
                d = os.path.join(dst_plots, item)
                if os.path.isfile(s):
                    shutil.copy2(s, d)
        print(f"[reports] saved to {report_dir}")
    except Exception as e:
        print(f"[reports] failed: {e}")

    max_dd_pct = max_dd_frac * 100.0
    mono_pct = mono_mag * 100.0

    print(
        f"equity_end_realized={equity_realized:.6f} equity_end_mtm={summary_dict['equity_end_mtm']:.6f} "
        f"trades={trades} pf={pf:.6f} fees_realized={fees_cum:.6f} "
        f"win_rate={win_rate_pct:.3f}% max_dd_mtm={max_dd_pct:.3f}% mono_mtm={mono_pct:.3f}% elapsed_sec={elapsed:.6f} "
        f"apr_realized={summary_dict['apr_%']:.3f}% daily_ret_realized={summary_dict['daily_return_%']:.3f}% "
        f"monthly_ret_realized={summary_dict['monthly_return_%']:.3f}% yearly_ret_realized={summary_dict['yearly_return_%']:.3f}% "
        f"sub_pnl_total={sub_pnl_cum:.6f} margin_call_excess_max={summary_dict['margin_call_excess_max']:.6f}"
    )

if __name__ == "__main__":
    main()
