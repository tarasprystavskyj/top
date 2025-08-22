# backtester_core_merged.py
# Unified core based on stable backtester_core.py, extended with:
# - run_backtest(...) reusable API
# - summary column rename: max_drawdown_% -> max_drawdown; win_rate_% -> win_rate
# - CLI flags: --print-summary (default True) and --silent (suppress stdout)
# - Colored equity_end (green/red) and extra metrics: daily_% and monthly_%
#
import argparse
import importlib
import yaml
from typing import Optional
import pandas as pd

from engine.data import load_cache, build_md_slice
from engine.portfolio import Portfolio

# --- Helpers ----------------------------------------------------------------

def _get(obj, key, default=None):
    if obj is None:
        return default
    if hasattr(obj, key):
        return getattr(obj, key)
    if isinstance(obj, dict):
        return obj.get(key, default)
    return default

def _act(adj):
    return _get(adj, "action")

def _new_stop(adj):
    return _get(adj, "new_stop")

def _new_tp(adj):
    return _get(adj, "new_tp")

def _reason(adj):
    return _get(adj, "reason", "exit")

def _sig_max_hold_hours(sig, fallback):
    val = _get(sig, "max_hold_hours")
    return fallback if (val is None) else val

def is_open_hour(t, open_hour_kyiv: int, kyiv_offset_hours: int) -> bool:
    utc_hour = (open_hour_kyiv - kyiv_offset_hours) % 24
    tt = t if t.tzinfo else pd.Timestamp(t, tz="UTC")
    return (tt.hour == utc_hour) and (tt.minute == 0)

def load_strategy(path_cls: str, cfg: dict):
    mod_path, cls_name = path_cls.rsplit(".", 1)
    mod = importlib.import_module(mod_path)
    cls = getattr(mod, cls_name)
    return cls(cfg)

def _rename_summary_columns(csv_path: str):
    try:
        df = pd.read_csv(csv_path)
        rename_map = {"max_drawdown_%": "max_drawdown", "win_rate_%": "win_rate"}
        intersect = {k: v for k, v in rename_map.items() if k in df.columns}
        if intersect:
            df = df.rename(columns=intersect)
            df.to_csv(csv_path, index=False)
    except Exception:
        pass

def _infer_bar_minutes(cfg_path: str, cfg: dict) -> int:
    import os
    name = os.path.basename(cfg_path).lower()
    patterns = [("5m",5),("10m",10),("15m",15),("30m",30),
                ("1h",60),("2h",120),("4h",240),("8h",480),("12h",720),("24h",1440),("1d",1440)]
    for tag, minutes in patterns:
        if tag in name:
            return minutes
    cache = str(cfg.get("cache_db","")).lower()
    for tag, minutes in patterns:
        if tag in cache:
            return minutes
    return 60

def _compute_elapsed_days(cfg_path: str, cfg: dict, limit_bars: int) -> Optional[float]:
    try:
        bar_min = _infer_bar_minutes(cfg_path, cfg)
        if limit_bars and limit_bars > 0:
            bars = int(limit_bars)
        else:
            try:
                dfs, all_times = load_cache(cfg["cache_db"])
                bars = len(all_times)
            except Exception:
                bars = 0
        return (bars * bar_min) / 1440.0 if bars > 0 else None
    except Exception:
        return None

def _print_summary(csv_path: str, days: Optional[float] = None, colorize: bool = True):
    try:
        df = pd.read_csv(csv_path)
        if len(df) == 1:
            row = df.iloc[0].to_dict()
            widest = max(len(str(k)) for k in row.keys())
            GREEN = "\x1b[32m"; RED = "\x1b[31m"; RESET = "\x1b[0m"
            es = row.get("equity_start"); ee = row.get("equity_end")
            def kv(key, val):
                print(f"{key.rjust(widest)} : {val}")
            kv("equity_start", es)
            if colorize and isinstance(es,(int,float)) and isinstance(ee,(int,float)):
                color = GREEN if ee > es else RED
                print(f"{'equity_end'.rjust(widest)} : {color}{ee}{RESET}")
            else:
                kv("equity_end", ee)
            for k in ["trades","profit_factor","max_drawdown","win_rate"]:
                if k in row:
                    kv(k, row[k])
            if days and isinstance(days,(int,float)) and days>0 and isinstance(es,(int,float)) and isinstance(ee,(int,float)) and es!=0:
                ratio = ee/es
                daily = ratio**(1.0/days) - 1.0
                monthly = (1.0 + daily)**30.0 - 1.0
                kv("elapsed_days", round(days, 6))
                kv("total_return_%", round((ratio-1.0)*100.0, 6))
                kv("daily_%", round(daily*100.0, 6))
                kv("monthly_%", round(monthly*100.0, 6))
        else:
            print(df.to_string(index=False))
    except Exception as e:
        print("[print-summary] failed:", e)

# --- Library API -------------------------------------------------------------

def run_backtest(cache_db: str,
                 strategy,
                 strategy_cfg: dict,
                 portfolio_cfg: dict,
                 session_cfg: dict,
                 limit_bars: int = 0):
    dfs, all_times = load_cache(cache_db)
    if limit_bars and limit_bars > 0:
        all_times = all_times[-int(limit_bars):]

    strat = load_strategy(strategy, strategy_cfg) if isinstance(strategy,str) else strategy(strategy_cfg)
    pf = Portfolio(portfolio_cfg)

    open_hour_kyiv = int(session_cfg.get("open_hour_kyiv", 1))
    kyiv_offset_hours = int(session_cfg.get("kyiv_offset_hours", 3))

    cooldown_days = int(strategy_cfg.get("cooldown_days", 3))
    default_hold_hours = int(strategy_cfg.get("hold_hours", 48))
    last_open_time = {}

    for t in all_times:
        tt = pd.Timestamp(t)
        t_utc = tt.tz_convert("UTC") if tt.tzinfo else pd.Timestamp(tt, tz="UTC")
        if not is_open_hour(t_utc, open_hour_kyiv, kyiv_offset_hours):
            continue

        md = build_md_slice(dfs, t_utc)
        univ = strat.universe(t_utc, md)
        ranked = strat.rank(t_utc, md, univ)

        for sym in ranked:
            lo = last_open_time.get(sym)
            if lo is not None and (t_utc - lo) < pd.Timedelta(days=cooldown_days):
                continue

            row = md.get(sym)
            if row is None:
                continue

            sig = strat.entry_signal(t_utc, sym, row, ctx={"portfolio": pf})
            if not sig:
                continue

            if not pf.can_open(portfolio_cfg):
                break

            pos = pf.open(sym, sig, t_utc, row["close"])
            pos.meta["max_hold_hours"] = _sig_max_hold_hours(sig, default_hold_hours)
            last_open_time[sym] = t_utc

        for pos in pf.open_positions():
            row = md.get(pos.symbol)
            if row is None:
                continue
            adj = strat.manage_position(t_utc, pos.symbol, pos, row, ctx={"portfolio": pf})
            action = _act(adj)
            if action == "EXIT":
                pf.close(pos, t_utc, row["close"], reason=_reason(adj))
            elif action == "MOVE_SL" and _new_stop(adj) is not None:
                pos.stop_price = _new_stop(adj)
            elif action == "MOVE_TP" and _new_tp(adj) is not None:
                pos.take_profit = _new_tp(adj)

    return pf

# --- CLI ---------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Unified backtester core")
    ap.add_argument("--cfg", type=str, default="configs/alpha_v0.yaml")
    ap.add_argument("--limit-bars", type=int, default=0)
    ap.add_argument("--print-summary", dest="print_summary", action="store_true", default=True,
                    help="Print summary.csv to stdout (default: on)")
    ap.add_argument("--silent", action="store_true", help="Suppress stdout (for grids)")
    args = ap.parse_args()

    def log(*a, **kw):
        if not args.silent:
            print(*a, **kw)

    cfg = yaml.safe_load(open(args.cfg, "r"))

    cache_db = cfg["cache_db"]
    strategy_spec = cfg["strategy_class"]
    strategy_cfg = cfg.get("strategy_params", {})
    portfolio_cfg = cfg["portfolio"]
    session_cfg = cfg.get("session", {})

    pf = run_backtest(cache_db=cache_db,
                      strategy=strategy_spec,
                      strategy_cfg=strategy_cfg,
                      portfolio_cfg=portfolio_cfg,
                      session_cfg=session_cfg,
                      limit_bars=args.limit_bars)

    pf.save_trades("trades.csv")
    pf.save_summary("summary.csv")
    _rename_summary_columns("summary.csv")

    if args.print_summary and not args.silent:
        days = _compute_elapsed_days(args.cfg, cfg, args.limit_bars)
        _print_summary("summary.csv", days=days, colorize=True)

    log("Saved trades.csv and summary.csv")

    if not args.silent:
        try:
            from engine.visualize_results import plot_equity_curves
            ret = plot_equity_curves(trades_csv="trades.csv",
                                     summary_csv="summary.csv",
                                     show=False,
                                     save_dir="plots",
                                     file_prefix="run")
            log("[visualize] saved", ret)
        except Exception as _e:
            log("[visualize] skipped:", _e)

if __name__ == "__main__":
    main()
