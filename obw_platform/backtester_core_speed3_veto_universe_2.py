#!/usr/bin/env python3
import argparse, sqlite3, importlib, time, sys, csv, os
from dataclasses import dataclass

import pathlib as _p
import yaml
import pandas as pd

def import_by_path(path: str):
    mod_name, cls_name = path.rsplit(".", 1)
    root = str((_p.Path(__file__).parent).resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    mod = importlib.import_module(mod_name)
    return getattr(mod, cls_name)

def connect_db(path: str):
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=OFF;")
    con.execute("PRAGMA synchronous=OFF;")
    con.execute("PRAGMA temp_store=MEMORY;")
    con.execute("PRAGMA mmap_size=268435456;")
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

def main():
    ap = argparse.ArgumentParser(description="Ultra-fast backtester (CSV + optional plots + DD/monotonicity)")
    ap.add_argument("--cfg", required=True)
    ap.add_argument("--limit-bars", type=int, default=500)
    ap.add_argument("--export-csv", dest="export_csv", action="store_true", help="Write trades.csv and summary.csv (default)")
    ap.add_argument("--no-export-csv", dest="export_csv", action="store_false", help="Disable CSV exports")
    ap.add_argument("--plots", dest="plots_dir", help="If set, save charts into this directory (e.g. 'plots')")
    ap.add_argument("--plots-dir", dest="plots_dir", help="Alias for --plots")
    # --- NEW: universe controls ---
    ap.add_argument("--symbols-file", dest="symbols_file",
                    help="Path to a file with allowed symbols (one per line). Overrides YAML universe_file if set.")
    ap.add_argument("--allow-symbols", dest="allow_symbols",
                    help="Comma-separated allowlist of symbols (overrides YAML universe_include).")
    ap.add_argument("--deny-symbols", dest="deny_symbols",
                    help="Comma-separated blocklist of symbols (overrides YAML universe_exclude).")
    ap.set_defaults(export_csv=True)
    args = ap.parse_args()

    t0 = time.time()
    cfg = yaml.safe_load(open(args.cfg))
    con = connect_db(cfg["cache_db"])

    # --- NEW: load universe allow/deny sets ---
    allow_syms, deny_syms = set(), set()

    # 1) CLI CSV lists
    allow_syms |= set(_split_csv_list(args.allow_symbols))
    deny_syms  |= set(_split_csv_list(args.deny_symbols))

    # 2) CLI file
    if args.symbols_file:
        try:
            with open(args.symbols_file, "r", encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if ln and not ln.startswith("#"):
                        allow_syms.add(ln)
        except Exception as e:
            print(f"[universe] failed to read --symbols-file: {e}")

    # 3) YAML file (only if CLI file not provided)
    if not args.symbols_file:
        u_file = cfg.get("universe_file")
        if u_file:
            try:
                with open(u_file, "r", encoding="utf-8") as f:
                    for ln in f:
                        ln = ln.strip()
                        if ln and not ln.startswith("#"):
                            allow_syms.add(ln)
            except Exception as e:
                print(f"[universe] failed to read universe_file: {e}")

    # 4) YAML inline lists
    allow_syms |= set(cfg.get("universe_include", []) or [])
    deny_syms  |= set(cfg.get("universe_exclude", []) or [])

    if allow_syms:
        print(f"[universe] allow list size = {len(allow_syms)}")
    if deny_syms:
        print(f"[universe] deny list  size = {len(deny_syms)}")

    # threshold time for last N distinct bars
    th_row = con.execute(
        "SELECT t FROM (SELECT DISTINCT datetime_utc AS t FROM price_indicators ORDER BY datetime_utc DESC LIMIT ?) ORDER BY t ASC LIMIT 1",
        (int(args.limit_bars),)
    ).fetchone()
    if not th_row:
        print("No times."); return
    min_time = th_row[0]

    # fetch rows >= min_time
    rows = con.execute(
        "SELECT symbol, datetime_utc, close, atr_ratio, dp6h, dp12h, quote_volume, qv_24h "
        "FROM price_indicators WHERE datetime_utc >= ? ORDER BY datetime_utc ASC, symbol ASC",
        (min_time,)
    ).fetchall()

    # bucket by time
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

    # strategy
    Strat = import_by_path(cfg["strategy_class"])
    strat = Strat(cfg)

    portfolio = cfg.get("portfolio", {})
    sp = cfg.get("strategy_params", {})
    initial_equity = float(portfolio.get("initial_equity", 100.0))
    pos_notional   = float(portfolio.get("position_notional", 20.0))
    fee      = float(portfolio.get("fee_rate", 0.001))
    slippage = float(portfolio.get("slippage_per_side", 0.0003))
    top_n    = int(sp.get("top_n", 8))
    side_pref= str(sp.get("side","BOTH")).upper()
    tp_mult  = float(sp.get("tp_atr_mult", 2.6))
    sl_mult  = float(sp.get("sl_atr_mult", 1.0))
    max_notional_frac = float(portfolio.get("max_notional_frac", 0.5))

    # prefilters
    min_atr = float(cfg.get('min_atr_ratio', 0.0))
    min_mom = float(cfg.get('min_momentum_sum', 0.0))
    min_qv24= float(cfg.get('min_qv_24h', 0.0))
    min_qv1h= float(cfg.get('min_qv_1h', 0.0))

    equity = initial_equity
    positions = {}
    pos_time = {}
    wins=losses=trades=0; pnl_pos=0.0; pnl_neg=0.0

    tr_rows = []  # trades.csv
    eq_curve_vals = [initial_equity]

    for t, bucket_all in slices:
        # Map цін для екзитів — БЕЗ фільтру!
        px_map = {sym: close for (sym, close, atr, dp6, dp12, qv1h, qv24) in bucket_all}

        # exits (без урахування юніверсу; закриватися завжди можна)
        if positions:
            for sym, pos in list(positions.items()):
                px = px_map.get(sym)
                if px is None: continue
                if pos.side=="LONG":
                    hit_tp = px >= pos.tp; hit_sl = px <= pos.sl
                    hit = hit_tp or hit_sl
                    gross_ret = (px - pos.entry)/pos.entry
                else:
                    hit_tp = px <= pos.tp; hit_sl = px >= pos.sl
                    hit = hit_tp or hit_sl
                    gross_ret = (pos.entry - px)/pos.entry
                if hit:
                    net_ret = gross_ret - 2*slippage - 2*fee
                    pnl = net_ret
                    trades+=1
                    if pnl>0: wins+=1; pnl_pos += pnl
                    else: losses+=1; pnl_neg += pnl
                    equity *= (1.0 + (pnl * pos_notional / equity))
                    eq_curve_vals.append(equity)
                    tr_rows.append({
                        "symbol": sym,
                        "side": pos.side,
                        "entry_time": pos_time.get(sym, t),
                        "exit_time": t,
                        "entry": pos.entry,
                        "exit": px,
                        "tp": pos.tp, "sl": pos.sl,
                        "reason": "TP" if hit_tp else "SL",
                        "gross_return": gross_ret,
                        "net_return": net_ret,
                        "notional": pos_notional,
                        "realized_pnl": net_ret * pos_notional,
                    })
                    del positions[sym]; pos_time.pop(sym, None)

        # --- NEW: фільтр юніверсу лише для відкриттів
        if allow_syms or deny_syms:
            bucket_open = [
                row for row in bucket_all
                if ((not allow_syms) or (row[0] in allow_syms)) and (row[0] not in deny_syms)
            ]
            if not bucket_open:
                # Нема допустимих символів для відкриття — ідемо далі
                continue
        else:
            bucket_open = bucket_all

        # rank top_n by momentum sum (на відкриттях використовуємо bucket_open)
        invert = (side_pref=="SHORT")
        best = []
        for idx, (sym, close, atr, dp6, dp12, qv1h, qv24) in enumerate(bucket_open):
            mom_sum = dp6 + dp12
            score = -mom_sum if invert else mom_sum
            if len(best)<top_n:
                best.append((score, idx))
                if len(best)==top_n:
                    best.sort(key=lambda x:x[0], reverse=True)
            else:
                if score > best[-1][0]:
                    best[-1] = (score, idx)
                    if best[-2][0] < best[-1][0]:
                        best.sort(key=lambda x:x[0], reverse=True)

        # opens
        row = {"close":0.0,"atr_ratio":0.0,"dp6h":0.0,"dp12h":0.0,"quote_volume":0.0,"qv_24h":0.0}
        shared_ctx = {}
        for _, idx in best:
            sym, close, atr, dp6, dp12, qv1h, qv24 = bucket_open[idx]
            if sym in positions: continue
            mom_sum = dp6 + dp12
            if atr < min_atr: continue
            if qv24 < min_qv24 or qv1h < min_qv1h: continue
            if side_pref in ('BOTH','LONG'):
                if mom_sum < min_mom: continue
                desired_side = "LONG"
            else:
                if -mom_sum < min_mom: continue
                desired_side = "SHORT"
            row["close"]=close; row["atr_ratio"]=atr; row["dp6h"]=dp6; row["dp12h"]=dp12; row["quote_volume"]=qv1h; row["qv_24h"]=qv24
            sig = strat.entry_signal(t, sym, row, ctx=shared_ctx)
            # Veto support
            if sig is None:
                continue
            take = getattr(sig, 'take', True)
            if not take:
                continue
            class _S: pass
            sig = sig or _S()
            if not hasattr(sig, 'side') or sig.side is None:
                sig.side = desired_side
            if getattr(sig, "side", desired_side) not in ("LONG","SHORT"):
                sig.side = desired_side
            if (len(positions)+1)*pos_notional > max_notional_frac * equity: break
            atr_abs = max(1e-12, atr*close)
            if sig.side=="LONG":
                sl = close - sl_mult*atr_abs; tp = close + tp_mult*atr_abs
            else:
                sl = close + sl_mult*atr_abs; tp = close - tp_mult*atr_abs
            positions[sym] = Position(sig.side, close, sl, tp)
            pos_time[sym] = t

    # mark-to-market
    if slices:
        last_t = slices[-1][0]
        last_px = {sym: close for (sym, close, atr, dp6, dp12, qv1h, qv24) in slices[-1][1]}
        for sym, pos in list(positions.items()):
            px = last_px.get(sym)
            if px is None: continue
            gross_ret = (px - pos.entry)/pos.entry if pos.side=="LONG" else (pos.entry - px)/pos.entry
            net_ret = gross_ret - 2*slippage - 2*fee
            pnl = net_ret
            trades += 1
            if pnl>0: wins+=1; pnl_pos += pnl
            else: losses+=1; pnl_neg += pnl
            equity *= (1.0 + (pnl * pos_notional / equity))
            eq_curve_vals.append(equity)
            tr_rows.append({
                "symbol": sym, "side": pos.side,
                "entry_time": pos_time.get(sym, last_t), "exit_time": last_t,
                "entry": pos.entry, "exit": px, "tp": pos.tp, "sl": pos.sl,
                "reason": "EOD", "gross_return": gross_ret, "net_return": net_ret,
                "notional": pos_notional, "realized_pnl": net_ret * pos_notional
            })
            del positions[sym]; pos_time.pop(sym, None)

    elapsed = time.time() - t0
    pf = (pnl_pos / max(1e-12, -pnl_neg)) if (pnl_pos>0 and pnl_neg<0) else 0.0

    # Max drawdown & monotonicity
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

    # CSV exports
    if args.export_csv:
        if tr_rows:
            cols = ["symbol","side","entry_time","exit_time","entry","exit","tp","sl","reason","gross_return","net_return","notional","realized_pnl"]
            with open("trades.csv","w",newline="") as f:
                w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(tr_rows)
        pd.DataFrame([{
            "equity_start": initial_equity, "equity_end": equity, "trades": trades,
            "profit_factor": pf, "win_rate_%": (wins*100.0/max(1,trades) if trades else 0.0),
            "elapsed_sec": elapsed,
            "max_dd_frac": max_dd_frac, "max_dd_%": (max_dd_frac*100.0),
            "monotonicity_sign": mono_sign, "monotonicity_mag": mono_mag
        }]).to_csv("summary.csv", index=False)

    # Optional plots
    if args.plots_dir:
        try:
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
            cfg_name = os.path.basename(args.cfg) if hasattr(args, 'cfg') else 'cfg:n/a'
            _dbn = os.path.basename(cfg.get('cache_db','')) if isinstance(cfg, dict) else ''
            _tf = '5m' if '5m' in _dbn else ('1440' if '1440' in _dbn else ('60m' if '60m' in _dbn else ('1h' if '1h' in _dbn else '?')))
            legend_label = f"{cfg_name} | TF: {_tf} | bars: {args.limit_bars}"
            os.makedirs(args.plots_dir, exist_ok=True)
            # Equity by trade
            import numpy as np
            eq_curve = np.array(eq_curve_vals, dtype=float)
            plt.figure(); plt.plot(range(len(eq_curve)), eq_curve, label=legend_label); plt.legend()
            plt.title("Equity vs Trade #"); plt.xlabel("Trade #"); plt.ylabel("Equity")
            plt.tight_layout(); plt.savefig(os.path.join(args.plots_dir, "equity_by_trade.png"), dpi=140); plt.close()

            # Drawdown by trade
            if len(eq_curve)>1:
                peaks = np.maximum.accumulate(eq_curve)
                dd = (eq_curve - peaks) / peaks
                plt.figure(); plt.plot(range(len(dd)), dd, label=legend_label); plt.legend()
                plt.title("Drawdown vs Trade #"); plt.xlabel("Trade #"); plt.ylabel("Drawdown (fraction)")
                plt.tight_layout(); plt.savefig(os.path.join(args.plots_dir, "drawdown_by_trade.png"), dpi=140); plt.close()

            # Equity vs Time (if timestamps present)
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
                ax.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d'))
                plt.xticks(rotation=45)
                plt.legend()

                plt.title("Equity vs Time"); plt.xlabel("Time"); plt.ylabel("Equity")
                plt.tight_layout(); plt.savefig(os.path.join(args.plots_dir, "equity_by_time.png"), dpi=160); plt.close()

            # Returns histogram
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

    print(f"equity_end={equity:.6f} trades={trades} pf={pf:.6f} max_dd={max_dd_frac:.6f} mono={mono_mag:.6f} elapsed_sec={elapsed:.6f}")

if __name__ == "__main__":
    main()