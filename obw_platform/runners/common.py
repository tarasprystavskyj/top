# Auto-generated common utilities extracted from bt_live_paper_runner.py
import os
import sys
import re
import json
import time
import math
import uuid
import argparse
import importlib
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any
import pandas as pd
import numpy as np

# ---------- optional engine imports (from backtester core) ----------
# ---------- optional engine imports (from backtester core) ----------
def _try_import(mod_path, names: List[str]):
    mod = importlib.import_module(mod_path)
    return [getattr(mod, n) for n in names]

EnginePortfolio = None
build_md_slice = None
load_cache = None


# ---------- CCXT fetchers ----------
# ---------- CCXT fetchers ----------
try:
    import ccxt  # type: ignore
except Exception:
    ccxt = None

RATE_MS = 130



def _tf_to_seconds(tf: str) -> int:
    tf = (tf or "1h").strip().lower()
    if tf.endswith("ms"):
        return max(1, int(tf[:-2])//1000)
    if tf.endswith("s"):
        return max(1, int(tf[:-1]))
    if tf.endswith("m"):
        return int(tf[:-1]) * 60
    if tf.endswith("h"):
        return int(tf[:-1]) * 3600
    if tf.endswith("d"):
        return int(tf[:-1]) * 86400
    if tf.endswith("w"):
        return int(tf[:-1]) * 7 * 86400
    if tf.endswith("m") and tf[:-1].isdigit():
        return int(tf[:-1]) * 60
    # default
    return 3600



def _align_bar_close(now_dt, tf_seconds: int):
    # floor to the nearest closed bar boundary
    epoch = int(now_dt.timestamp())
    aligned = epoch - (epoch % tf_seconds)
    from datetime import datetime, timezone
    return datetime.fromtimestamp(aligned, tz=timezone.utc)



Key features
- One config for all modes (YAML/JSON), same strategy_class across modes
- PAPER (db): replays from a cache DB (price_indicators schema) using engine.* APIs
- PAPER-API (NEW): pulls live prices from BingX (or a MockFetcher in --dry-run),
  simulates orders, writes Orders/Decisions/Equity to a session DB,
  and emits a cache-out DB (price_indicators) of exactly-what-we-saw
- LIVE: mirrors your live bot flow (BingX + reduce-only brackets in hedge mode)
- BACKTEST: delegates to backtester_core.py with the provided cfg

Example
Backtest:
  python3 bt_live_paper_runner.py --mode backtest --cfg configs/cs_C2_base_1h.yaml --limit-bars 500

Paper (DB):
  python3 bt_live_paper_runner.py --mode paper --paper-source db --cfg configs/cs_C2_base_1h.yaml \
    --db combined_cache.db --results-dir paper_results --limit-bars 168

Paper (API, simulated orders + session DB + cache-out):
  python3 bt_live_paper_runner.py --mode paper --paper-source api --cfg configs/cs_C2_base_1h.yaml \
    --exchange bingx --symbol-format usdtm --poll-sec 15 --bar-delay-sec 10 --limit_klines 180 \
    --results-dir paper_api_results --orders-db paper_api_results/session.sqlite \
    --session-db paper_api_results/session.sqlite \
    --cache-out paper_api_results/combined_cache_session.db

Paper (API DRY RUN, no internet, generates synthetic OHLCV, 1 step):
  python3 bt_live_paper_runner.py --mode paper --paper-source api --cfg dryrun_lenient.yaml \
    --results-dir paper_api_dryrun --orders-db paper_api_dryrun/session.sqlite \
    --session-db paper_api_dryrun/session.sqlite --cache-out paper_api_dryrun/combined_cache_session.db \
    --dry-run --iterations 1
"""
import os
import sys
import re
import json
import time
import math
import uuid
import argparse
import importlib
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

import pandas as pd
import numpy as np

try:
    import yaml  # optional
except Exception:
    yaml = None

# ---------- optional engine imports (from backtester core) ----------


def _try_import(mod_path, names: List[str]):
    mod = importlib.import_module(mod_path)
    return [getattr(mod, n) for n in names]

EnginePortfolio = None
build_md_slice = None
load_cache = None
try:
    EnginePortfolio, = _try_import("engine.portfolio", ["Portfolio"])
    build_md_slice, load_cache = _try_import("engine.data", ["build_md_slice", "load_cache"])
except Exception:
    EnginePortfolio = None
    build_md_slice = None
    load_cache = None

# ---------- helpers ----------


def load_yaml_or_json(path: str) -> dict:
    if not path:
        return {}
    try:
        if path.endswith(".json"):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        if yaml is not None:
            with open(path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
    except Exception as e:
        print(f"[cfg] failed to parse {path}: {e}", file=sys.stderr)
    # naive fallback
    cfg = {}
    try:
        for line in open(path, "r", encoding="utf-8").read().splitlines():
            if ":" in line and not line.strip().startswith("#"):
                k, v = line.split(":", 1)
                vv = v.strip()
                if vv.lower() in ("true", "false"):
                    cfg[k.strip()] = (vv.lower() == "true")
                else:
                    try:
                        if "." in vv or "e" in vv.lower():
                            cfg[k.strip()] = float(vv)
                        else:
                            cfg[k.strip()] = int(vv)
                    except Exception:
                        cfg[k.strip()] = vv
    except Exception:
        pass
    return cfg



def mask(s: str) -> str:
    s = str(s or "")
    if len(s) <= 4:
        return "*" * len(s)
    return s[:2] + "*" * (len(s) - 4) + s[-2:]



def sleep_ms(ms: int):
    time.sleep(max(0.0, float(ms) / 1000.0))

# ---------- strategy loading ----------


def compute_feats(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    prev_close = out["close"].shift(1)
    tr = pd.concat(
        [(out["high"] - out["low"]).abs(),
         (out["high"] - prev_close).abs(),
         (out["low"] - prev_close).abs()], axis=1
    ).max(axis=1)
    atr = tr.ewm(alpha=1 / 14.0, adjust=False).mean()
    out["atr_ratio"] = (atr / out["close"]).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    out["quote_volume"] = (out["volume"] * out["close"]).fillna(0.0)
    out["qv_24h"] = out["quote_volume"].rolling(24, min_periods=1).sum()
    out["dp6h"] = (out["close"] / out["close"].shift(6) - 1.0).fillna(0.0)
    out["dp12h"] = (out["close"] / out["close"].shift(12) - 1.0).fillna(0.0)
    # placeholders for compatibility
    for k in ("rsi", "stochastic", "mfi", "overbought_index", "gain_24h_before"):
        if k not in out.columns:
            out[k] = 0.0
    return out

# ---------- CCXT fetchers ----------
try:
    import ccxt  # type: ignore
except Exception:
    ccxt = None

RATE_MS = 130



class CCXTFetcher:
    def __init__(self, exchange="bingx", symbol_format="usdtm", debug=False, logfile=""):
        if not ccxt:
            raise RuntimeError("ccxt is not installed. pip install 'ccxt<5'")
        self.debug = debug
        self.logfile = logfile
        self.ex = getattr(ccxt, exchange)({"enableRateLimit": True, "timeout": 20000})
        try:
            self.markets = self.ex.load_markets()
        except Exception as e:
            self.markets = {}
            print(f"[ccxt load_markets] {e}", file=sys.stderr)

        self.by_base: Dict[str, str] = {}
        for m in self.markets.values():
            try:
                if m.get("swap") and m.get("quote") == "USDT":
                    b = m.get("base")
                    if b:
                        self.by_base[b] = m["symbol"]
            except Exception:
                continue

    def resolve_symbol(self, s: str) -> Optional[str]:
        if s in self.markets:
            return s
        if s in self.by_base:
            return self.by_base[s]
        u = s.upper().replace("-", "/").replace("USDTUSDT", "USDT:USDT")
        u = u.replace(":USDTUSDT", ":USDT")
        for cand in (u, u.replace("/USDT", "/USDT:USDT"), u.replace("/USDT:USDT", "/USDT")):
            if cand in self.markets:
                return cand
        b = s.split("/", 1)[0].split("-", 1)[0].replace("USDT", "")
        if b in self.by_base:
            return self.by_base[b]
        return None

    def fetch_ohlcv_df(self, sym: str, timeframe="1h", limit=180) -> Optional[pd.DataFrame]:
        ccxt_sym = self.resolve_symbol(sym) or sym
        try:
            data = self.ex.fetch_ohlcv(ccxt_sym, timeframe=timeframe, limit=limit)
            sleep_ms(RATE_MS)
        except Exception as e:
            print(f"[fetch_ohlcv] {sym}: {e}", file=sys.stderr)
            return None
        if not data:
            return None
        df = pd.DataFrame(data, columns=["ts", "open", "high", "low", "close", "volume"])
        df["datetime_utc"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df = df.set_index("datetime_utc")[["open", "high", "low", "close", "volume"]].astype(float)
        return df

    def fetch_ticker_price(self, sym: str) -> Optional[float]:
        ccxt_sym = self.resolve_symbol(sym) or sym
        try:
            t = self.ex.fetch_ticker(ccxt_sym)
            sleep_ms(RATE_MS)
            p = float(t.get("last") or t.get("close") or 0.0)
            return p if p > 0 else None
        except Exception as e:
            print(f"[ticker] {sym}: {e}", file=sys.stderr)
            return None



def fallback_load_cache_sqlite(db_path: str):
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in cur.fetchall()]
    table = None
    for t in tables:
        try:
            cur.execute(f"PRAGMA table_info({t})")
            cols = [r[1].lower() for r in cur.fetchall()]
            if set(["symbol", "datetime_utc", "close"]).issubset(set(cols)):
                table = t
                break
        except Exception:
            continue
    if table is None:
        con.close()
        raise RuntimeError("No suitable table in DB (need symbol, datetime_utc, close)")
    df = pd.read_sql_query(f"SELECT * FROM {table}", con)
    con.close()
    df["datetime_utc"] = pd.to_datetime(df["datetime_utc"], utc=True, errors="coerce")
    df = df.dropna(subset=["datetime_utc"]).sort_values(["datetime_utc", "symbol"])
    times = sorted(df["datetime_utc"].unique().tolist())
    dfs = {}
    for sym, sub in df.groupby("symbol"):
        sub = sub.set_index("datetime_utc").sort_index()
        dfs[sym] = sub
    return dfs, times



def safe_load_cache(db_path: str):
    if load_cache is not None:
        try:
            return load_cache(db_path)
        except Exception as e:
            print(f"[fallback] engine.data.load_cache failed: {e}", file=sys.stderr)
    return fallback_load_cache_sqlite(db_path)



def safe_build_md_slice(dfs: dict, t):
    if build_md_slice is not None:
        try:
            return build_md_slice(dfs, t)
        except Exception as e:
            print(f"[fallback] engine.data.build_md_slice failed: {e}", file=sys.stderr)
    out = {}
    for sym, df in dfs.items():
        row = df[df.index <= t].tail(1)
        if row is None or len(row) == 0:
            continue
        out[sym] = row.iloc[0].to_dict()
    return out

# ---------- features ----------


def ensure_orders_db(path: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path)
    cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS orders(
        order_id TEXT PRIMARY KEY,
        ts_utc TEXT,
        bar_time_utc TEXT,
        mode TEXT,
        symbol TEXT,
        side TEXT,
        type TEXT,
        price REAL,
        qty REAL,
        status TEXT,
        reason TEXT,
        run_id TEXT,
        extra TEXT
    )""")
    con.commit()
    con.close()



def insert_order_row(db_path: str, row: dict):
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cols = ["order_id", "ts_utc", "bar_time_utc", "mode", "symbol", "side", "type",
            "price", "qty", "status", "reason", "run_id", "extra"]
    vals = [row.get(c) for c in cols]
    cur.execute(f"INSERT OR REPLACE INTO orders({','.join(cols)}) VALUES({','.join(['?']*len(cols))})", vals)
    con.commit()
    con.close()

# ---------- Session DB & Cache-out helpers ----------


def ensure_session_dbs(results_dir: str, session_db: str = "", cache_out: str = ""):
    sess = session_db or os.path.join(results_dir, "session.sqlite")
    cachep = cache_out or os.path.join(results_dir, "combined_cache_session.db")
    os.makedirs(os.path.dirname(sess) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(cachep) or ".", exist_ok=True)

    con = sqlite3.connect(sess)
    cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS config_snapshots(
        run_id TEXT, ts_utc TEXT, cfg_json TEXT, PRIMARY KEY(run_id, ts_utc)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS decisions(
        run_id TEXT, bar_time_utc TEXT, universe_size INTEGER, ranked_json TEXT, selected_json TEXT,
        PRIMARY KEY(run_id, bar_time_utc)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS equity(
        run_id TEXT, ts_utc TEXT, equity_usdt REAL, cash_usdt REAL, position_value_usdt REAL,
        realized_pnl_cum REAL, unrealized_pnl REAL,
        PRIMARY KEY(run_id, ts_utc)
    )""")
    con.commit()
    con.close()

    con2 = sqlite3.connect(cachep)
    cur2 = con2.cursor()
    cur2.execute("""CREATE TABLE IF NOT EXISTS price_indicators(
        symbol TEXT, datetime_utc TEXT,
        open REAL, high REAL, low REAL, close REAL, volume REAL,
        rsi REAL, stochastic REAL, mfi REAL, overbought_index REAL,
        atr_ratio REAL, gain_24h_before REAL, dp6h REAL, dp12h REAL,
        quote_volume REAL, qv_24h REAL, vol_surge_mult REAL,
        PRIMARY KEY(symbol, datetime_utc)
    )""")
    con2.commit()
    con2.close()
    return sess, cachep



def write_config_snapshot(sess_path: str, run_id: str, cfg: dict):
    con = sqlite3.connect(sess_path)
    cur = con.cursor()
    cur.execute("INSERT OR REPLACE INTO config_snapshots(run_id, ts_utc, cfg_json) VALUES(?,?,?)",
                (run_id, datetime.utcnow().isoformat(), json.dumps(cfg)))
    con.commit()
    con.close()



def write_decisions(sess_path: str, run_id: str, bar_time, ranked_list, selected_list):
    con = sqlite3.connect(sess_path)
    cur = con.cursor()
    cur.execute("INSERT OR REPLACE INTO decisions(run_id, bar_time_utc, universe_size, ranked_json, selected_json) VALUES(?,?,?,?,?)",
                (run_id, bar_time.isoformat(), int(len(ranked_list)), json.dumps(list(ranked_list)), json.dumps(list(selected_list))))
    con.commit()
    con.close()



def write_equity(sess_path: str, run_id: str, t, equity_dict: dict):
    con = sqlite3.connect(sess_path)
    cur = con.cursor()
    cur.execute("""INSERT OR REPLACE INTO equity(run_id, ts_utc, equity_usdt, cash_usdt, position_value_usdt,
                    realized_pnl_cum, unrealized_pnl)
                    VALUES(?,?,?,?,?,?,?)""",
                (run_id, t.isoformat(),
                 float(equity_dict.get("equity", 0.0)),
                 float(equity_dict.get("cash", 0.0)),
                 float(equity_dict.get("position_value", 0.0)),
                 float(equity_dict.get("realized_pnl_cum", 0.0)),
                 float(equity_dict.get("unrealized_pnl", 0.0))))
    con.commit()
    con.close()



def cache_out_upsert(cache_path: str, symbol: str, feats_df: pd.DataFrame):
    con = sqlite3.connect(cache_path)
    cur = con.cursor()
    cols = ["symbol","datetime_utc","open","high","low","close","volume",
            "rsi","stochastic","mfi","overbought_index","atr_ratio","gain_24h_before",
            "dp6h","dp12h","quote_volume","qv_24h","vol_surge_mult"]
    placeholders = ",".join(["?"] * len(cols))
    for idx, r in feats_df.iterrows():
        row = [
            symbol,
            pd.to_datetime(idx, utc=True).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            float(r.get("open", 0.0)), float(r.get("high", 0.0)), float(r.get("low", 0.0)),
            float(r.get("close", 0.0)), float(r.get("volume", 0.0)),
            float(r.get("rsi", 0.0)), float(r.get("stochastic", 0.0)), float(r.get("mfi", 0.0)), float(r.get("overbought_index", 0.0)),
            float(r.get("atr_ratio", 0.0)), float(r.get("gain_24h_before", 0.0)),
            float(r.get("dp6h", 0.0)), float(r.get("dp12h", 0.0)),
            float(r.get("quote_volume", 0.0)), float(r.get("qv_24h", 0.0)), float(r.get("vol_surge_mult", 0.0))
        ]
        cur.execute(f"INSERT OR REPLACE INTO price_indicators({','.join(cols)}) VALUES({placeholders})", row)
    con.commit()
    con.close()

# ---------- PAPER runner (DB replay) ----------


def qty_for_notional(mkt: dict, notional: float, price: float):
    min_qty = float(mkt.get("limits", {}).get("amount", {}).get("min") or 0.0)
    step = float(mkt.get("precision", {}).get("amount") or 0.0)
    min_notional_req = float(mkt.get("limits", {}).get("cost", {}).get("min") or 0.0)
    if step and step > 0:
        qty = max(min_qty, math.floor(notional / max(price, 1e-9) / step) * step)
    else:
        qty = max(min_qty, notional / max(price, 1e-9))
    return qty, min_notional_req, step, min_qty



def place_open_long(fetcher: CCXTFetcher, sym: str, notional: float, price: float, position_mode: str):
    ccxt_sym = fetcher.resolve_symbol(sym)
    mkt = fetcher.markets.get(ccxt_sym, {})
    qty, min_notional_req, step, min_qty = qty_for_notional(mkt, notional, price)
    if min_notional_req > notional + 1e-9:
        return {"ok": False, "skip_reason": f"min_notional {min_notional_req:.6g} > {notional:.6g}", "qty_rules": {"min_qty": min_qty, "step": step}}
    params = {"reduceOnly": False}
    if position_mode == "hedge":
        params["positionSide"] = "LONG"
    try:
        od = fetcher.ex.create_order(ccxt_sym, "market", "buy", qty, None, params)
        sleep_ms(RATE_MS)
        return {"ok": True, "order": od, "qty": qty, "params": params}
    except Exception as e:
        msg = str(e)
        if "Min amount" in msg and step > 0:
            try:
                qty = max(min_qty, qty + step)
                od = fetcher.ex.create_order(ccxt_sym, "market", "buy", qty, None, params)
                sleep_ms(RATE_MS)
                return {"ok": True, "order": od, "qty": qty, "params": params, "retry": True}
            except Exception as e2:
                return {"ok": False, "error": str(e2), "qty": qty, "params": params}
        return {"ok": False, "error": msg, "qty": qty, "params": params}



def place_reduce_only(fetcher: CCXTFetcher, sym: str, side_close: str, qty: float, position_mode: str):
    ccxt_sym = fetcher.resolve_symbol(sym)
    params = {"reduceOnly": True}
    if position_mode == "hedge":
        params["positionSide"] = "LONG"
    try:
        od = fetcher.ex.create_order(ccxt_sym, "market", side_close, qty, None, params)
        sleep_ms(RATE_MS)
        return od
    except Exception as e:
        print(f"[live reduceOnly] {sym}: {e}", file=sys.stderr)
        return None

