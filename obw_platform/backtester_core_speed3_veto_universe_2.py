
#!/usr/bin/env python3
# backtester_core_speed3_veto_universe_refactored.py
# Thin backtester:
#   - Universe allow/deny (OPENINGS only)
#   - Calls strat.universe()/rank() -> candidates (strategy owns top_n, filters)
#   - Calls strat.entry_signal() -> must return TP/SL; we just attach to Position
#   - Calls strat.manage_position() for exits
#   - Heat is reporting-only (printed if provided), not used for decisions.
import argparse, sqlite3, importlib, time, sys, os, pathlib as _p, shutil, json
from dataclasses import dataclass
from typing import Dict, Any

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

def _split_csv_list(s):
    if not s: return []
    return [x.strip() for x in str(s).split(",") if x.strip()]
    

def find_db_file(filename: str):
    """Locate a SQLite cache DB file.

    The original implementation only searched a handful of hard coded
    locations.  Allow callers to provide an absolute path (or a relative path
    with directories) which is returned as-is if it exists.  This enables
    running backtests against databases stored outside of the repository, such
    as live session caches.
    """

    if os.path.isabs(filename) and os.path.exists(filename):
        return os.path.abspath(filename)

    # Список можливих шляхів
    search_paths = [
        os.path.join(".", filename),           # ./filename
        os.path.join("..", filename),          # ../filename
        os.path.join("..", "DB", filename),    # ../DB/filename
    ]

    for path in search_paths:
        if os.path.exists(path):
            return os.path.abspath(path)  # повертаємо повний шлях

    raise FileNotFoundError(f"DB file '{filename}' not found in {search_paths}")


def _timeframe_to_minutes(tf: str) -> float:
    s = str(tf).strip().lower()
    if s.endswith("m"):
        try:
            return float(s[:-1])
        except Exception:
            return 0.0
    if s.endswith("h"):
        try:
            return float(s[:-1]) * 60.0
        except Exception:
            return 0.0
    if s.endswith("d"):
        try:
            return float(s[:-1]) * 1440.0
        except Exception:
            return 0.0
    try:
        return float(s)
    except Exception:
        return 0.0


def _norm_iso(ts: str):
    if not ts:
        return ts
    try:
        return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).isoformat()
    except Exception:
        return ts

def main():
    ap = argparse.ArgumentParser(description="Thin backtester (universe + strategy-owned logic)")
    ap.add_argument("--cfg", required=True)
    ap.add_argument("--limit-bars", type=int, default=500)
    ap.add_argument("--time-from", dest="time_from", type=str, default=None)
    ap.add_argument("--time-to", dest="time_to", type=str, default=None)
    ap.add_argument("--allow-symbols", type=str, default="")
    ap.add_argument("--plots", dest="plots_dir", type=str, default=None)
    ap.add_argument("--export-csv", action="store_true")
    ap.add_argument("--debug", action="store_true")
    # Universe controls (OPENINGS)
    ap.add_argument("--symbols-file", dest="symbols_file")
    ap.add_argument("--deny-symbols", dest="deny_symbols")
    ap.add_argument("--cache_db", dest="cache_db")
    args = ap.parse_args()

    t0 = time.time()
    cfg = yaml.safe_load(open(args.cfg, "r"))
    cache_db = args.cache_db or cfg["cache_db"]
    db_file = find_db_file(cache_db)
    con = _db_connect(db_file)

    # Allow/Deny sets
    allow_syms, deny_syms = set(), set()
    allow_syms |= set(_split_csv_list(args.allow_symbols))
    deny_syms  |= set(_split_csv_list(args.deny_symbols))

    sym_file = args.symbols_file or cfg.get("symbols_file") or cfg.get("universe_file")
    if sym_file:
        if not os.path.isabs(sym_file):
            # Users sometimes provide paths like "universe/foo.txt".  Previously we
            # blindly prefixed our own "universe" directory which resulted in
            # paths such as "universe/universe/foo.txt".  Normalise the supplied
            # path by keeping only the filename and joining it with the local
            # "universe" directory.
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
    t_from = _norm_iso(getattr(args, 'time_from', None))
    t_to   = _norm_iso(getattr(args, 'time_to', None))
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
            """
            SELECT MIN(datetime_utc), MAX(datetime_utc), COUNT(*)
            FROM price_indicators WHERE datetime_utc BETWEEN ? AND ?
            """,
            (
                t_from,
                t_to,
            ),
        ).fetchone()
        time_start, time_end, rows_count = rr[0], rr[1], rr[2]
        if args.debug:
            print(f"[dbg] rows_in_range={rows_count} db_min={time_start} db_max={time_end}")
    else:
        # Determine the earliest timestamp for the requested number of bars.
        #
        # Previously we simply grabbed the last ``limit_bars`` rows from the
        # ``price_indicators`` table.  Because the table stores one row per
        # symbol per timestamp, limiting by rows resulted in an extremely
        # narrow time window when many symbols were present (e.g. 500 rows over
        # 100 symbols yields only five minutes of data).  This caused the
        # backtester to always operate on a very small, fixed interval.
        #
        # To honour ``--limit-bars`` we instead select the latest *distinct*
        # timestamps and compute the minimum among them.  When an allow-list is
        # provided, we restrict the search to those symbols so extraneous rows
        # do not skew the range.
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

    equity = initial_equity
    positions: Dict[str, Position] = {}
    pos_time: Dict[str, str] = {}
    wins=losses=trades=0; pnl_pos=0.0; pnl_neg=0.0; fees_cum=0.0
    tr_rows: list[Dict[str, Any]] = []
    eq_curve_vals = [initial_equity]

    for t, bucket_all in slices:
        # Price map for exits
        px_map = {sym: close for (sym, close, *_rest) in bucket_all}

        # Exits (strategy-owned)
        if positions:
            for sym, pos in list(positions.items()):
                row = None
                for tup in bucket_all:
                    if tup[0]==sym:
                        sym2, close, atr, dp6, dp12, qv1h, qv24 = tup
                        row = {"close":close,"atr_ratio":atr,"dp6h":dp6,"dp12h":dp12,"quote_volume":qv1h,"qv_24h":qv24}
                        break
                if row is None: continue
                ex = strat.manage_position(sym, row, pos, ctx=None)
                if ex and ex.action in ("TP","SL","EXIT"):
                    px = float(ex.exit_price if ex.exit_price is not None else row["close"])
                    gross_ret = (px - pos.entry)/pos.entry if pos.side=="LONG" else (pos.entry - px)/pos.entry
                    net_ret = gross_ret - 2*slippage - 2*fee
                    pnl = net_ret
                    trades+=1
                    fees_cum += fee * 2 * pos_notional
                    if pnl>0: wins+=1; pnl_pos += pnl
                    else: losses+=1; pnl_neg += pnl
                    equity *= (1.0 + (pnl * pos_notional / equity))
                    tr_rows.append({
                        "symbol": sym,
                        "side": pos.side,
                        "entry_time": pos_time.get(sym, t),
                        "exit_time": t,
                        "entry": pos.entry,
                        "exit": px,
                        "tp": pos.tp, "sl": pos.sl,
                        "reason": ex.action,
                        "gross_return": gross_ret,
                        "net_return": net_ret,
                        "notional": pos_notional,
                        "fees_paid": fee * 2 * pos_notional,
                        "realized_pnl": net_ret * pos_notional,
                    })
                    del positions[sym]; pos_time.pop(sym, None)

        # compute current equity including unrealized PnL
        unrealized = 0.0
        for sym, pos in positions.items():
            px = px_map.get(sym)
            if px is None:
                continue
            if pos.side == "LONG":
                gross_ret = (px - pos.entry) / pos.entry
            else:
                gross_ret = (pos.entry - px) / pos.entry
            net_ret = gross_ret - 2 * slippage - 2 * fee
            unrealized += net_ret * pos_notional
        equity_mtm = equity + unrealized

        # --- Universe filtering for OPENINGS only (allow/deny) ---
        # Build md_map for all symbols in this bucket
        md_map_all = {
            sym: {"close":close,"atr_ratio":atr,"dp6h":dp6,"dp12h":dp12,"quote_volume":qv1h,"qv_24h":qv24}
            for (sym, close, atr, dp6, dp12, qv1h, qv24) in bucket_all
        }
        if allow_syms or deny_syms:
            md_map_open = {s:row for s,row in md_map_all.items()
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
            # Budget check
            if (len(positions)+1)*pos_notional > max_notional_frac * equity_mtm:
                break
            row = md_map_open.get(sym)
            if not row:
                continue
            sig = strat.entry_signal(True, sym, row, ctx=None)
            if sig is None:
                continue
            # Validate Sig (no backtester fallbacks)
            if sig.side not in ("LONG","SHORT"):
                raise RuntimeError(f"Strategy must supply side LONG/SHORT for {sym}")
            tp = getattr(sig, "take_profit", getattr(sig, "tp_price", getattr(sig, "tp", None)))
            sl = getattr(sig, "stop_price", getattr(sig, "sl_price", getattr(sig, "sl", None)))
            if not isinstance(tp, (int,float)) or not isinstance(sl, (int,float)):
                raise RuntimeError(f"Strategy must supply numeric take_profit/stop_price for {sym}")
            entry_px = float(row["close"])
            positions[sym] = Position(sig.side, entry_px, float(sl), float(tp))
            pos_time[sym] = t
            # Heat reporting (if strategy exposes it) — optional, for logs only
            heat = None
            try:
                if hasattr(strat, "heat"):
                    heat = strat.heat(t, sym, row)
            except Exception:
                heat = None
            #if heat is not None:
            #    print(f"[open] {t} {sym} {sig.side} heat={heat:.3f} tp={tp:.4f} sl={sl:.4f}")

        # snapshot equity after this bar
        eq_curve_vals.append(equity_mtm)

    # Mark-to-market finalization
    if slices:
        last_t = slices[-1][0]
        last_px = {sym: close for (sym, close, *_rest) in slices[-1][1]}
        for sym, pos in list(positions.items()):
            px = last_px.get(sym)
            if px is None: continue
            gross_ret = (px - pos.entry)/pos.entry if pos.side=="LONG" else (pos.entry - px)/pos.entry
            net_ret = gross_ret - 2*slippage - 2*fee
            pnl = net_ret
            trades += 1
            fees_cum += fee * 2 * pos_notional
            if pnl>0: wins+=1; pnl_pos += pnl
            else: losses+=1; pnl_neg += pnl
            equity *= (1.0 + (pnl * pos_notional / equity))
            eq_curve_vals.append(equity)
            tr_rows.append({
                "symbol": sym, "side": pos.side,
                "entry_time": pos_time.get(sym, last_t), "exit_time": last_t,
                "entry": pos.entry, "exit": px, "tp": pos.tp, "sl": pos.sl,
                "reason": "EOD", "gross_return": gross_ret, "net_return": net_ret,
                "notional": pos_notional, "fees_paid": fee * 2 * pos_notional,
                "realized_pnl": net_ret * pos_notional
            })
            del positions[sym]; pos_time.pop(sym, None)

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
    total_return = (equity / initial_equity) if initial_equity else 0.0
    if total_days > 0 and total_return > 0:
        daily_ret = total_return ** (1.0 / total_days) - 1.0
        monthly_ret = total_return ** (1.0 / (total_days / 30.0)) - 1.0 if total_days >= 1 else 0.0
        yearly_ret = total_return ** (1.0 / (total_days / 365.0)) - 1.0 if total_days >= 1 else 0.0
        apr = ((equity - initial_equity) / initial_equity) * (365.0 / total_days)
    else:
        daily_ret = monthly_ret = yearly_ret = apr = 0.0
    apr_pct = apr * 100.0
    daily_ret_pct = daily_ret * 100.0
    monthly_ret_pct = monthly_ret * 100.0
    yearly_ret_pct = yearly_ret * 100.0

    # Metrics
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
        "equity_end": equity,
        "trades": trades,
        "profit_factor": pf,
        "win_rate_%": win_rate_pct,
        "elapsed_sec": elapsed,
        "max_dd_frac": max_dd_frac,
        "max_dd_%": (max_dd_frac * 100.0),
        "monotonicity_sign": mono_sign,
        "monotonicity_mag": mono_mag,
        "total_fees": fees_cum,
        "apr_%": apr_pct,
        "daily_return_%": daily_ret_pct,
        "monthly_return_%": monthly_ret_pct,
        "yearly_return_%": yearly_ret_pct,
    }

    cfg_name = os.path.splitext(os.path.basename(args.cfg))[0] if hasattr(args, 'cfg') else 'cfg'
    time_id = time.strftime("%Y%m%d_%H%M%S")
    report_dir = os.path.join("_reports", "_backtest", f"backtest{cfg_name}{time_id}")

    # CSV exports
    if args.export_csv:
        os.makedirs(report_dir, exist_ok=True)
        trades_path = os.path.join(report_dir, "bt_trades.csv")
        summary_path = os.path.join(report_dir, "bt_summary.csv")
        trades_df = pd.DataFrame(tr_rows)
        trades_df.to_csv(trades_path, index=False)
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary_dict, f, indent=2, default=str)
        print(f"[files] bt_trades={trades_path} bt_summary={summary_path}")

    # Plots (same as before)
    if args.plots_dir:
        try:
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
            cfg_name = os.path.basename(args.cfg) if hasattr(args, 'cfg') else 'cfg:n/a'
            _dbn = os.path.basename(cfg.get('cache_db','')) if isinstance(cfg, dict) else ''
            _tf = '5m' if '5m' in _dbn else ('1440' if '1440' in _dbn else ('60m' if '60m' in _dbn else ('1h' if '1h' in _dbn else '?')))
            legend_label = f"{cfg_name} | TF: {_tf} | bars: {bars_count}"
            os.makedirs(args.plots_dir, exist_ok=True)

            import numpy as np
            eq_curve = np.array(eq_arr, dtype=float)
            plt.figure(); plt.plot(range(len(eq_curve)), eq_curve, label=legend_label); plt.legend()
            plt.title("Equity vs Trade #"); plt.xlabel("Trade #"); plt.ylabel("Equity")
            plt.tight_layout(); plt.savefig(os.path.join(args.plots_dir, "equity_by_trade.png"), dpi=140); plt.close()

            if len(eq_curve)>1:
                peaks = np.maximum.accumulate(eq_curve)
                dd = (eq_curve - peaks) / peaks
                plt.figure(); plt.plot(range(len(dd)), dd, label=legend_label); plt.legend()
                plt.title("Drawdown vs Trade #"); plt.xlabel("Trade #"); plt.ylabel("Drawdown (fraction)")
                plt.tight_layout(); plt.savefig(os.path.join(args.plots_dir, "drawdown_by_trade.png"), dpi=140); plt.close()

            if tr_rows and tr_rows[0].get("exit_time", None) is not None:
                dft = pd.DataFrame(tr_rows).sort_values("exit_time")
                eq_time = (float(initial_equity) + dft["realized_pnl"].cumsum())
                plt.figure()
                ts_raw = dft['exit_time'].astype(str).str.strip()
                ts = pd.Series(pd.NaT, index=dft.index)
                for _fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                    parsed = pd.to_datetime(ts_raw, format=_fmt, errors='coerce')
                    ts = ts.fillna(parsed)
                try:
                    mask = ts.isna()
                    if mask.any():
                        ts.loc[mask] = pd.to_datetime(ts_raw[mask], format='mixed', errors='coerce')
                except Exception:
                    pass
                mask = ts.isna()
                if mask.any():
                    ts.loc[mask] = ts_raw[mask].map(lambda x: pd.to_datetime(x, errors='coerce'))
                plt.plot(ts, eq_time.values, label=legend_label)
                ax = plt.gca()
                ax.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d %H'))
                plt.xticks(rotation=45)
                plt.legend()

                plt.title("Equity vs Time"); plt.xlabel("Time"); plt.ylabel("Equity")
                plt.tight_layout(); plt.savefig(os.path.join(args.plots_dir, "equity_by_time.png"), dpi=160); plt.close()

            if tr_rows:
                dfr = pd.DataFrame(tr_rows)
                series = None
                if "net_return" in dfr:
                    series = pd.to_numeric(dfr["net_return"], errors="coerce").dropna()
                elif "gross_return" in dfr:
                    series = pd.to_numeric(dfr["gross_return"], errors="coerce").dropna()
                if series is not None and len(series)>0:
                    plt.figure(); plt.hist(series.values, bins=30)
                    plt.title("Distribution of Returns per Trade"); plt.xlabel("Return per trade"); plt.ylabel("Count")
                    plt.tight_layout(); plt.savefig(os.path.join(args.plots_dir, "returns_hist.png"), dpi=140); plt.close()
        except Exception as e:
            print(f"[plots] failed: {e}")

    # Consolidate reports
    try:
        os.makedirs(report_dir, exist_ok=True)
        for fname in ("trades.csv", "summary.csv"):
            if os.path.exists(fname):
                shutil.copy(fname, report_dir)
        if args.plots_dir and os.path.isdir(args.plots_dir):
            plots_abs = os.path.abspath(args.plots_dir)
            report_abs = os.path.abspath(report_dir)
            try:
                common = os.path.commonpath([plots_abs, report_abs])
            except Exception:
                common = ""
            if common != plots_abs:
                for item in os.listdir(args.plots_dir):
                    if item.startswith("backtest"):
                        continue
                    src = os.path.join(args.plots_dir, item)
                    dst = os.path.join(report_dir, item)
                    if os.path.isdir(src):
                        shutil.copytree(src, dst, dirs_exist_ok=True)
                    elif os.path.isfile(src):
                        shutil.copy(src, report_dir)
            else:
                print(f"[reports] skip copying from plots_dir={plots_abs} (ancestor of report_dir)")
        print(f"[reports] saved to {report_dir}")
    except Exception as e:
        print(f"[reports] failed: {e}")

    max_dd_pct = max_dd_frac * 100.0
    mono_pct = mono_mag * 100.0
    print(
        f"equity_end={equity:.6f} trades={trades} pf={pf:.6f} fees={fees_cum:.6f} "
        f"win_rate={win_rate_pct:.3f}% max_dd={max_dd_pct:.3f}% mono={mono_pct:.3f}% elapsed_sec={elapsed:.6f} "
        f"apr={apr_pct:.3f}% daily_ret={daily_ret_pct:.3f}% monthly_ret={monthly_ret_pct:.3f}% yearly_ret={yearly_ret_pct:.3f}%"
    )

if __name__ == "__main__":
    main()
