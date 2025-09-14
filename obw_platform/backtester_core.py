# backtester_core.py — unified core (Py3.8-compatible, memory-light, safe DB fallback)
import argparse
import importlib
import os
import sys
import yaml
import pandas as pd
from typing import Optional

from engine.data import load_cache, build_md_slice
from engine.portfolio import Portfolio

# ---------- logging helpers -----------
def _mklog(silent: bool):
    def log(*a, **kw):
        if not silent:
            print(*a, **kw)
    def err(*a, **kw):
        if not silent:
            print(*a, file=sys.stderr, **kw)
    return log, err

# ---------- small utils ---------------
def _get(obj, key, default=None):
    if obj is None:
        return default
    if hasattr(obj, key):
        return getattr(obj, key)
    if isinstance(obj, dict):
        return obj.get(key, default)
    return default

def _act(adj): return _get(adj, "action")
def _new_stop(adj): return _get(adj, "new_stop")
def _new_tp(adj): return _get(adj, "new_tp")
def _reason(adj): return _get(adj, "reason", "exit")

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
        inter = {k: v for k, v in rename_map.items() if k in df.columns}
        if inter:
            df = df.rename(columns=inter)
            df.to_csv(csv_path, index=False)
    except Exception:
        pass  # cosmetic

def _downcast_numeric(df):
    try:
        fcols = df.select_dtypes(include=["float64"]).columns
        icols = df.select_dtypes(include=["int64"]).columns
        for c in fcols: df[c] = pd.to_numeric(df[c], downcast="float")
        for c in icols: df[c] = pd.to_numeric(df[c], downcast="integer")
    except Exception:
        pass
    return df

# ---------- TF inference & DB fallback ----------
def _infer_bar_minutes(cfg_path: str, cfg: dict) -> int:
    name = os.path.basename(cfg_path).lower() if cfg_path else ""
    patterns = [("5m",5),("10m",10),("15m",15),("30m",30),("1h",60),
                ("2h",120),("4h",240),("8h",480),("12h",720),("24h",1440),("1d",1440)]
    for tag, minutes in patterns:
        if tag in name: return minutes
    cache = str(cfg.get("cache_db","")).lower()
    for tag, minutes in patterns:
        if tag in cache: return minutes
    return 60

def _guess_cache_filename(cfg_path: str, cfg: dict) -> str:
    tf = _infer_bar_minutes(cfg_path, cfg)
    if tf == 5: return "combined_cache_5m_1440.db"
    if tf == 120: return "combined_cache_1440_2h.db"
    if tf == 240: return "combined_cache_1440_4h.db"
    if tf == 480: return "combined_cache_1440_8h.db"
    return "combined_cache_1440.db"

def _resolve_cache_db(cache_db: str, cfg_path: str, cfg: dict, db_dir_cli: str = None) -> str:
    # 0) try path relative to YAML config dir
    if cache_db:
        cfg_dir = os.path.dirname(os.path.abspath(cfg_path)) if cfg_path else os.getcwd()
        cand_rel = os.path.join(cfg_dir, cache_db)
        if os.path.exists(cand_rel):
            return cand_rel
    # 0.5) explicit --db-dir has priority over env/fallback
    if db_dir_cli:
        cand_cli = os.path.join(db_dir_cli, os.path.basename(cache_db) if cache_db else _guess_cache_filename(cfg_path, cfg))
        if os.path.exists(cand_cli):
            return cand_cli
    # 1) absolute or existing relative
    if cache_db and os.path.isabs(cache_db) and os.path.exists(cache_db): return cache_db
    if cache_db and os.path.exists(cache_db): return cache_db
    # 2) fallbacks
    dirs = []
    envd = os.getenv("BACKTEST_DB_DIR")
    if envd: dirs.append(envd)
    dirs.append("/var/www/vps2.happyuser.info/top")
    names = []
    if cache_db: names.append(os.path.basename(cache_db))
    names.append(_guess_cache_filename(cfg_path, cfg))
    for d in dirs:
        for n in names:
            if not n: continue
            cand = os.path.join(d, n)
            if os.path.exists(cand): return cand
    return cache_db


import sqlite3

def _detect_table_sqlite(db_path: str) -> str:
    con = sqlite3.connect(db_path)
    try:
        cur = con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        names = [r[0] for r in cur.fetchall()]
        def table_score(tbl):
            try:
                cols = [r[1].lower() for r in con.execute(f"PRAGMA table_info({tbl})").fetchall()]
            except Exception:
                cols = []
            time_candidates = ["time","timestamp","ts","open_time","close_time","kline_open_time","kline_close_time","t","date","dt","datetime","datetime_utc","time_ms","start_time","time_open","time_close","bar_time","bar_start"]
            symbol_candidates = ["symbol","ticker","pair","asset","sym","instrument","base"]
            has_time = any(c in cols for c in time_candidates) or any(("time" in c or "date" in c or "datetime" in c) for c in cols)
            has_symbol = any(c in cols for c in symbol_candidates)
            return (1 if has_time else 0) + (1 if has_symbol else 0)
        preferences = ["price_indicators","klines","candles","ohlcv"]
        for pref in preferences:
            if pref in names and table_score(pref) >= 1:
                return pref
        if names:
            scored = [(t, table_score(t)) for t in names]
            scored.sort(key=lambda x: x[1], reverse=True)
            return scored[0][0]
        raise RuntimeError("No tables in cache DB")
    finally:
        con.close()

def _detect_time_symbol_columns(con, table: str):
    try:
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]
    except Exception:
        cols = []
    cl = [c.lower() for c in cols]
    time_candidates = ["time","timestamp","ts","open_time","close_time","kline_open_time","kline_close_time","t","date","dt","datetime","datetime_utc","time_ms","start_time","time_open","time_close","bar_time","bar_start"]
    symbol_candidates = ["symbol","ticker","pair","asset","sym","instrument","base"]
    time_col = None
    for c in time_candidates:
        if c in cl:
            time_col = cols[cl.index(c)]; break
    if time_col is None:
        for i, cname in enumerate(cl):
            if ('time' in cname) or ('date' in cname) or ('datetime' in cname):
                time_col = cols[i]; break
    symbol_col = None
    for c in symbol_candidates:
        if c in cl: symbol_col = cols[cl.index(c)]; break
    return time_col, symbol_col, cols

def _canonical_select_columns(all_cols: list, time_col: str, symbol_col: str):
    wanted = [time_col]
    if symbol_col: wanted.append(symbol_col)
    common = ["open","high","low","close","quote_volume","volume","vol"]
    al = [c.lower() for c in all_cols]
    for name in common:
        if name in al: wanted.append(all_cols[al.index(name)])
    out, seen = [], set()
    for x in wanted:
        if x and x not in seen:
            out.append(x); seen.add(x)
    return out

def _unit_ms_or_s(max_ts: int) -> str:
    return "ms" if max_ts and int(max_ts) > 10**12 else "s"

def _load_cache_tail_safe(db_path: str, limit_bars: int):
    con = sqlite3.connect(db_path)
    try:
        table = _detect_table_sqlite(db_path)
        time_col, symbol_col, all_cols = _detect_time_symbol_columns(con, table)
        if not time_col:
            raise RuntimeError("No time-like column found in table %s" % table)
        select_cols = _canonical_select_columns(all_cols, time_col, symbol_col)
        sel = ",".join(select_cols)
        dfs = {}
        all_times_set = set()
        # get symbol list or None
        syms = [None]
        if symbol_col:
            syms = [r[0] for r in con.execute(f"SELECT DISTINCT {symbol_col} FROM {table}")]
        for sym in syms:
            if symbol_col:
                q = f"SELECT {sel} FROM {table} WHERE {symbol_col}=? ORDER BY {time_col} DESC LIMIT ?"
                params = (sym, int(limit_bars))
            else:
                q = f"SELECT {sel} FROM {table} ORDER BY {time_col} DESC LIMIT ?"
                params = (int(limit_bars),)
            df = pd.read_sql_query(q, con, params=params)
            if df.empty: continue
            # normalize cols
            df.columns = [c.lower() for c in df.columns]
            if "time" not in df.columns:
                df = df.rename(columns={time_col.lower(): "time"})
            # to datetime index
            if pd.api.types.is_numeric_dtype(df["time"]):
                unit = _unit_ms_or_s(int(df["time"].max()))
                df["time"] = pd.to_datetime(df["time"].astype("int64"), unit=unit, utc=True)
            else:
                df["time"] = pd.to_datetime(df["time"], utc=True, errors="coerce")
            df = df.sort_values("time").set_index("time")
            if symbol_col and symbol_col.lower() in df.columns and "symbol" not in df.columns:
                df = df.rename(columns={symbol_col.lower(): "symbol"})
            # downcast
            df = _downcast_numeric(df)
            key = sym if sym is not None else "__ALL__"
            dfs[key] = df
            all_times_set.update(df.index.tolist())
        all_times = pd.DatetimeIndex(sorted(all_times_set), tz="UTC")
        return dfs, all_times, table, time_col, symbol_col, len(syms)
    finally:
        con.close()

def _estimate_elapsed_days_from_db(db_path: str) -> Optional[float]:
    try:
        con = sqlite3.connect(db_path)
        try:
            table = _detect_table_sqlite(db_path)
            time_col, _scol, _all = _detect_time_symbol_columns(con, table)
            if not time_col: return None
            row = con.execute(f"SELECT MIN({time_col}), MAX({time_col}) FROM {table}").fetchone()
            if not row: return None
            mn, mx = row
            def _to_int(v):

                try: return int(v)
                except Exception: return None
            ts_mx = _to_int(mx)
            if ts_mx is not None:
                unit = _unit_ms_or_s(ts_mx)
                tmin = pd.to_datetime(int(mn), unit=unit, utc=True)
                tmax = pd.to_datetime(int(mx), unit=unit, utc=True)
            else:
                tmin = pd.to_datetime(mn, utc=True, errors="coerce")
                tmax = pd.to_datetime(mx, utc=True, errors="coerce")
            if pd.isna(tmin) or pd.isna(tmax): return None
            return float((tmax - tmin).total_seconds()) / 86400.0
        finally:
            con.close()
    except Exception:
        return None

# ---------- console summary --------------
def _print_summary(csv_path: str, days: Optional[float] = None, colorize: bool = True):
    try:
        df = pd.read_csv(csv_path)
        if len(df) == 1:
            row = df.iloc[0].to_dict()
            widest = max(len(str(k)) for k in row.keys())
            GREEN="\x1b[32m"; RED="\x1b[31m"; RESET="\x1b[0m"
            es = row.get("equity_start"); ee = row.get("equity_end")
            def kv(k, v): print(f"{k.rjust(widest)} : {v}")
            kv("equity_start", es)
            if colorize and isinstance(es,(int,float)) and isinstance(ee,(int,float)):
                color = GREEN if ee > es else RED
                print(f"{'equity_end'.rjust(widest)} : {color}{ee}{RESET}")
            else:
                kv("equity_end", ee)
            for k in ["trades","profit_factor","max_drawdown","win_rate"]:
                if k in row: kv(k, row[k])
            if (days is not None) and isinstance(days,(int,float)) and days>0 and isinstance(es,(int,float)) and isinstance(ee,(int,float)) and es!=0:
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

# ---------- core runner ------------------
def run_backtest(cache_db: str,
                 strategy,
                 strategy_cfg: dict,
                 portfolio_cfg: dict,
                 session_cfg: dict,
                 limit_bars: int = 0,
                 silent: bool = False,
                 cfg_path: str = "config.yaml",
                 cfg_all: dict = None,
                 mem_light: bool = True,
                 max_universe: int = 0,
                 db_dir_cli: str = None,
                 debug: bool = False):
    log, err = _mklog(silent)

    # Resolve DB path & try to load; catch "No tables in cache DB"
    path = _resolve_cache_db(cache_db, cfg_path, cfg_all or {}, db_dir_cli)
    if debug and not silent:
        print(f"[db] resolved: {path}")
    try:
        if limit_bars and int(limit_bars) > 0:
            dfs, all_times, tbl, tcol, scol, n_syms = _load_cache_tail_safe(path, int(limit_bars))
            if debug and not silent:
                print(f"[db] table={tbl}, time_col={tcol}, symbol_col={scol}, symbols={n_syms}")
        else:
            dfs, all_times = load_cache(path)
    except Exception as e:
        # last-resort retries with guessed filename
        guessed = _guess_cache_filename(cfg_path, cfg_all or {})
        for d in [os.getenv("BACKTEST_DB_DIR"), "/var/www/vps2.happyuser.info/top"]:
            if not d: continue
            p = os.path.join(d, guessed)
            if os.path.exists(p):
                if not silent: print("[db-fallback] retrying:", p)
                if limit_bars and int(limit_bars) > 0:
                    dfs, all_times, tbl, tcol, scol, n_syms = _load_cache_tail_safe(p, int(limit_bars))
                    if debug and not silent:
                        print(f"[db] table={tbl}, time_col={tcol}, symbol_col={scol}, symbols={n_syms}")
                else:
                    dfs, all_times = load_cache(p)
                break
        else:
            raise

    # Optional memory downcast
    if mem_light:
        try:
            for k in list(dfs.keys()):
                dfs[k] = _downcast_numeric(dfs[k])
        except Exception:
            pass

    if limit_bars and int(limit_bars) > 0:
        all_times = all_times[-int(limit_bars):]

    # Strategy instance
    strat = load_strategy(strategy, strategy_cfg) if isinstance(strategy, str) else strategy(strategy_cfg)
    pf = Portfolio(portfolio_cfg)

    open_hour_kyiv = int(session_cfg.get("open_hour_kyiv", 1))
    kyiv_offset_hours = int(session_cfg.get("kyiv_offset_hours", 3))
    open_every_bar = bool(session_cfg.get("open_every_bar", False))

    cooldown_days = int(strategy_cfg.get("cooldown_days", 3))
    default_hold_hours = int(strategy_cfg.get("hold_hours", 48))
    last_open_time = {}

    proc_count = 0
    for t in all_times:
        tt = pd.Timestamp(t)
        t_utc = tt.tz_convert("UTC") if tt.tzinfo else pd.Timestamp(tt, tz="UTC")
        if (not open_every_bar) and (not is_open_hour(t_utc, open_hour_kyiv, kyiv_offset_hours)):
            continue

        md = build_md_slice(dfs, t_utc)
        univ = strat.universe(t_utc, md)
        ranked = strat.rank(t_utc, md, univ)
        if isinstance(max_universe, int) and max_universe > 0:
            ranked = ranked[:max_universe]

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
            elif action == "TP_PARTIAL":
                part = max(0.0, min(1.0, float(_get(adj, "qty_frac", 0.5))))
                price = float(row.get("close", 0.0))
                qty_close = (pos.notional / max(pos.entry_price, 1e-12)) * part
                notional = qty_close * price
                min_notional = _get(strat, "exchange_min_notional", 0.0)
                min_qty = _get(strat, "min_qty", 0.0)
                if notional >= min_notional and (min_qty <= 0 or qty_close >= min_qty):
                    pf.close_partial(pos, t_utc, price, part, reason=_reason(adj))
                else:
                    pass
            elif action == "MOVE_SL" and _new_stop(adj) is not None:
                pos.stop_price = _new_stop(adj)
            elif action == "MOVE_TP" and _new_tp(adj) is not None:
                pos.take_profit = _new_tp(adj)

        proc_count += 1
        if proc_count % 50 == 0:
            try:
                import gc as _gc; _gc.collect()
            except Exception:
                pass

    return pf

# ---------- CLI -------------------------
def _compute_elapsed_days(cfg_path: str, cfg: dict, limit_bars: int, silent: bool, db_dir_cli: str = None) -> Optional[float]:
    try:
        bar_min = _infer_bar_minutes(cfg_path, cfg)
        if limit_bars and int(limit_bars) > 0:
            return (int(limit_bars) * bar_min) / 1440.0
        # else: cheap estimate from DB min/max time
        path = _resolve_cache_db(cfg.get("cache_db",""), cfg_path, cfg, db_dir_cli)
        days = _estimate_elapsed_days_from_db(path)
        return days
    except Exception:
        return None

def main():
    ap = argparse.ArgumentParser(description="Unified backtester core (Py3.8-compatible)")
    ap.add_argument("--cfg", type=str, default="configs/alpha_v0.yaml")
    ap.add_argument("--limit-bars", type=int, default=0)
    ap.add_argument("--print-summary", dest="print_summary", action="store_true", default=True,
                    help="Print summary.csv to stdout (default: on)")
    ap.add_argument("--silent", action="store_true", help="Suppress stdout (for grids)")
    ap.add_argument("--no-plot", action="store_true", help="Skip plotting to save memory/CPU")
    ap.add_argument("--mem-light", dest="mem_light", action="store_true", default=True,
                    help="Downcast numeric columns to reduce RAM (default: on)")
    ap.add_argument("--no-mem-light", dest="mem_light", action="store_false",
                    help="Disable numeric downcast")
    ap.add_argument("--max-universe", type=int, default=0, help="Process at most N ranked symbols per decision (0 = no cap)")
    ap.add_argument("--db-dir", type=str, default=None, help="Directory to search cache DB in (priority over BACKTEST_DB_DIR)")
    ap.add_argument("--debug", action="store_true", help="Verbose diagnostics (DB path, table/columns, symbol count)")
    args = ap.parse_args()

    log, err = _mklog(args.silent)

    cfg = yaml.safe_load(open(args.cfg, "r"))

    pf = run_backtest(cache_db=cfg["cache_db"],
                      strategy=cfg["strategy_class"],
                      strategy_cfg=cfg.get("strategy_params", {}),
                      portfolio_cfg=cfg["portfolio"],
                      session_cfg=cfg.get("session", {}),
                      limit_bars=args.limit_bars,
                      silent=args.silent,
                      cfg_path=args.cfg,
                      cfg_all=cfg,
                      mem_light=args.mem_light,
                      max_universe=args.max_universe,
                      db_dir_cli=args.db_dir,
                      debug=args.debug)

    pf.save_trades("trades.csv")
    pf.save_summary("summary.csv")
    _rename_summary_columns("summary.csv")

    # Print console summary
    if args.print_summary and not args.silent:
        days = _compute_elapsed_days(args.cfg, cfg, args.limit_bars, args.silent, db_dir_cli=args.db_dir)
        _print_summary("summary.csv", days=days, colorize=True)

    log("Saved trades.csv and summary.csv")

    # Optional visualization
    if not args.silent and not args.no_plot:
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
