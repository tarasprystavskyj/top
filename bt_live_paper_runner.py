#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bt_live_paper_runner.py
-----------------------
Unified runner for BACKTEST, PAPER (db), PAPER-API (live prices, simulated fills),
and LIVE (real orders on BingX via CCXT), using strategies from the `strategies/` folder.

Enhancements vs the 1h sample:
- Proper multi-timeframe support (e.g., 5m): bar rounding by TF, TF-aware feature windows
- PAPER-API also respects --hour-cache {off,save,load} (as in LIVE)
- CCXT timeframe shim: if exchange lacks exact TF, fetch a divisor TF and aggregate
- Progress dots preserved in PAPER-API & LIVE
"""

import os
import sys
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
    # naive fallback (best-effort)
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

def parse_timeframe_to_seconds(tf: str) -> int:
    tf = (tf or "1h").strip()
    unit = tf[-1]
    try:
        n = int(tf[:-1])
    except Exception:
        n = 1
    mult = {"m": 60, "h": 3600, "d": 86400, "w": 7*86400, "M": 30*86400}
    return n * mult.get(unit, 3600)

def round_bar_close(now_utc: datetime, tf: str) -> datetime:
    sec = parse_timeframe_to_seconds(tf)
    t = int(now_utc.timestamp())
    close = t - (t % sec)
    return datetime.fromtimestamp(close, tz=timezone.utc)

# ---------- strategy loading ----------
def load_strategy(path_cls: str, cfg: dict):
    mod_path, cls_name = path_cls.rsplit(".", 1)
    mod = importlib.import_module(mod_path)
    cls = getattr(mod, cls_name)
    return cls(cfg)

# ---------- DB fallback loaders for PAPER (db) ----------
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

# ---------- features (TF-aware) ----------
def compute_feats(df: pd.DataFrame, tf_seconds: Optional[int] = None) -> pd.DataFrame:
    """
    Computes a minimal set of features; windows scale with timeframe.
    If tf_seconds is None, defaults to 1h semantics (backward compatible).
    """
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

    if tf_seconds is None:
        # 1h legacy: 24/6/12 bars
        out["qv_24h"] = out["quote_volume"].rolling(24, min_periods=1).sum()
        out["dp6h"] = (out["close"] / out["close"].shift(6) - 1.0).fillna(0.0)
        out["dp12h"] = (out["close"] / out["close"].shift(12) - 1.0).fillna(0.0)
        # crude vol surge (legacy)
        avg1 = out["qv_24h"] / 24.0
        with np.errstate(divide="ignore", invalid="ignore"):
            out["vol_surge_mult"] = np.where(avg1 > 0, out["quote_volume"] / avg1, 0.0)
    else:
        bars_24h = max(1, int(round(24*3600 / tf_seconds)))
        bars_6h  = max(1, int(round( 6*3600 / tf_seconds)))
        bars_12h = max(1, int(round(12*3600 / tf_seconds)))
        out["qv_24h"] = out["quote_volume"].rolling(bars_24h, min_periods=1).sum()
        out["dp6h"]  = (out["close"] / out["close"].shift(bars_6h)  - 1.0).fillna(0.0)
        out["dp12h"] = (out["close"] / out["close"].shift(bars_12h) - 1.0).fillna(0.0)
        avg_per_bar = out["qv_24h"] / float(bars_24h)
        with np.errstate(divide="ignore", invalid="ignore"):
            out["vol_surge_mult"] = np.where(avg_per_bar > 0, out["quote_volume"] / avg_per_bar, 0.0)

    # placeholders for compatibility
    for k in ("rsi", "stochastic", "mfi", "overbought_index", "gain_24h_before"):
        if k not in out.columns:
            out[k] = 0.0
    return out


def _print_heat_from_strategy(strat, mode_label: str, bar_close, md: dict, uni_syms):
    """
    Use strategy.best_entry_distance(...) if available to print 'heat' (distance to entry)
    using thresholds from strategy YAML. Safe no-op if strategy lacks the helper.
    """
    try:
        best_entry_distance = getattr(strat, "best_entry_distance", None)
        if best_entry_distance is None:
            return False
        symbols = list(uni_syms) if uni_syms else list(md.keys())
        best = best_entry_distance(bar_close, md, symbols=symbols)
        # Always print a live-like line for time context when opened==0
        print(f"[{mode_label}] opened=0 at {bar_close.isoformat()}")
        if not best:
            return True
        heat = max(0.0, 1.0 - float(best.get("combined_gap", 1.0)))
        g = best.get("gaps", {}) or {}
        th = best.get("thresholds", {}) or {}
        a = best.get("actuals", {}) or {}
        print(f"[heat] nearest={best.get('symbol')} heat={heat*100:.1f}% (lower gap is closer to entry)")
        if "atr" in g:
            print(f"       atr_ratio {a.get('atr_ratio',0.0):.6f} vs min {th.get('min_atr_ratio',0.0):.6f} -> gap {g.get('atr',0.0)*100:.1f}%")
        if "volsurge" in g:
            print(f"       vol_surge_mult req×avg1h -> gap {g.get('volsurge',0.0)*100:.1f}%")
        if "qv24" in g:
            print(f"       qv_24h {a.get('qv_24h',0.0):.0f} vs min {th.get('min_qv_24h',0.0):.0f} -> gap {g.get('qv24',0.0)*100:.1f}%")
        if "qv1h" in g:
            print(f"       qv_1h  {a.get('qv_1h',0.0):.0f} vs min {th.get('min_qv_1h',0.0):.0f} -> gap {g.get('qv1h',0.0)*100:.1f}%")
        if "momentum" in g:
            print(f"       momentum {a.get('mom_sum',0.0):.4f} vs req {th.get('min_momentum_sum',0.0):.4f} -> gap {g.get('momentum',0.0)*100:.1f}%")
        if "breadth" in g:
            print(f"       breadth  {a.get('breadth',0.0):.3f} vs min {th.get('min_breadth',0.0):.3f} -> gap {g.get('breadth',0.0)*100:.1f}%")
        return True
    except Exception:
        return False

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
        api_k = os.environ.get("BINGX_KEY") or os.environ.get("API_KEY")
        api_s = os.environ.get("BINGX_SECRET") or os.environ.get("API_SECRET")
        opts = {"enableRateLimit": True, "timeout": 20000}
        if api_k and api_s:
            opts.update({"apiKey": api_k, "secret": api_s})
        self.ex = getattr(ccxt, exchange)(opts)
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

    def _choose_fetch_tf(self, req_tf: str):
        """Pick a supported TF to fetch and the aggregation factor."""
        try:
            exchange_tfs = set(getattr(self.ex, "timeframes", {}) or {})
        except Exception:
            exchange_tfs = set()
        fetch_tf = req_tf
        agg = 1
        if exchange_tfs and req_tf not in exchange_tfs:
            # try to find a divisor TF to aggregate
            def sec(tf): 
                try: return int(self.ex.parse_timeframe(tf))
                except Exception: return parse_timeframe_to_seconds(tf)
            s_req = sec(req_tf)
            cands = []
            for tf in exchange_tfs:
                s = sec(tf)
                if s <= s_req and s_req % s == 0:
                    cands.append((s, tf))
            if cands:
                cands.sort()
                s_fetch, fetch_tf = cands[-1]
                agg = s_req // s_fetch
            else:
                # fallback to nearest smaller
                smaller = [(sec(tf), tf) for tf in exchange_tfs if sec(tf) < s_req]
                if smaller:
                    smaller.sort()
                    s_fetch, fetch_tf = smaller[-1]
                    agg = max(1, s_req // s_fetch)
        return fetch_tf, agg

    def fetch_ohlcv_df(self, sym: str, timeframe="1h", limit=180) -> Optional[pd.DataFrame]:
        ccxt_sym = self.resolve_symbol(sym) or sym
        fetch_tf, agg = self._choose_fetch_tf(timeframe)
        try:
            data = self.ex.fetch_ohlcv(ccxt_sym, timeframe=fetch_tf, limit=int(limit)*int(agg)+5)
            sleep_ms(RATE_MS)
        except Exception as e:
            print(f"[fetch_ohlcv] {sym}: {e}", file=sys.stderr)
            return None
        if not data:
            return None
        df = pd.DataFrame(data, columns=["ts", "open", "high", "low", "close", "volume"])
        if agg > 1:
            slot_ms = parse_timeframe_to_seconds(timeframe) * 1000
            df["slot"] = (df["ts"] // slot_ms) * slot_ms
            df = df.groupby("slot", as_index=False).agg({
                "ts":"max","open":"first","high":"max","low":"min","close":"last","volume":"sum"
            })
        df["datetime_utc"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df = df.set_index("datetime_utc")[["open", "high", "low", "close", "volume"]].astype(float)
        return df.sort_index().tail(limit)

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

class MockFetcher:
    """Network-free fetcher for DRY RUN. Generates synthetic OHLCV for a small universe."""
    def __init__(self, symbols=None):
        if symbols is None:
            symbols = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]
        self.by_base = {s.split("/")[0]: s for s in symbols}

    def resolve_symbol(self, s: str):
        if s in self.by_base.values():
            return s
        base = s.split("/", 1)[0].split("-", 1)[0].upper().replace("USDT", "")
        return self.by_base.get(base, list(self.by_base.values())[0])

    def fetch_ohlcv_df(self, sym: str, timeframe="1h", limit=180):
        now = datetime.utcnow().replace(tzinfo=timezone.utc)
        sec = parse_timeframe_to_seconds(timeframe)
        idx = [now - timedelta(seconds=sec*(limit - i)) for i in range(limit)]
        idx = pd.to_datetime(idx, utc=True)
        base = 100.0 + (hash(sym) % 300)
        steps = np.random.normal(0, 0.5, size=limit).cumsum()
        close = base + steps
        high = close + np.abs(np.random.normal(0.3, 0.2, size=limit))
        low = close - np.abs(np.random.normal(0.3, 0.2, size=limit))
        openp = close + np.random.normal(0, 0.2, size=limit)
        vol = np.abs(np.random.normal(100, 25, size=limit))
        df = pd.DataFrame({"open": openp, "high": high, "low": low, "close": close, "volume": vol}, index=idx)
        return df

    def fetch_ticker_price(self, sym: str):
        df = self.fetch_ohlcv_df(sym, limit=2)
        return float(df["close"].iloc[-1])

# ---------- Orders table helpers ----------
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

def read_hour_cache_row(cache_path: str, symbol: str, dt_utc) -> dict:
    """Return feature dict for exact bar time from price_indicators, or {} if missing."""
    try:
        con = sqlite3.connect(cache_path)
        cur = con.cursor()
        ts = dt_utc.isoformat()
        row = cur.execute(
            "SELECT open,high,low,close,volume,rsi,stochastic,mfi,overbought_index,atr_ratio,gain_24h_before,dp6h,dp12h,quote_volume,qv_24h,vol_surge_mult "
            "FROM price_indicators WHERE symbol=? AND datetime_utc=? LIMIT 1", (symbol, ts)
        ).fetchone()
        con.close()
        if not row:
            return {}
        keys = ["open","high","low","close","volume","rsi","stochastic","mfi","overbought_index","atr_ratio","gain_24h_before","dp6h","dp12h","quote_volume","qv_24h","vol_surge_mult"]
        return {k: float(v) for k, v in zip(keys, row)}
    except Exception:
        return {}

# ---------- PAPER runner (DB replay) ----------
def run_paper(db_path: str, cfg: dict, results_dir: str, limit_bars: Optional[int] = None):
    assert EnginePortfolio is not None, "engine.portfolio.Portfolio unavailable (place script in repo root)"
    strat_path = cfg.get("strategy_class", "strategies.cross_sectional_rs.CrossSectionalRS")
    strat = load_strategy(strat_path, cfg)

    dfs, times = safe_load_cache(db_path)
    if limit_bars is not None and limit_bars > 0:
        times = times[-int(limit_bars):]

    port_cfg = {
        "initial_equity": float(cfg.get("initial_equity", cfg.get("start_cash", 200.0))),
        "fee_rate": float(cfg.get("fee_rate", 0.0006)),
        "slippage_per_side": float(cfg.get("slippage_per_side", cfg.get("slip_bps", 1.5) / 10000.0
                                           if isinstance(cfg.get("slip_bps", 0), (int, float)) else 0.0003)),
        "tick_pct": float(cfg.get("tick_pct", 0.0001)),
        "position_notional": float(cfg.get("notional", 2.2)),
        "max_notional_frac": float(cfg.get("max_notional_frac", 0.5)),
        "funding_rate_hour": float(cfg.get("funding_rate_hour", 0.0)),
    }
    pf = EnginePortfolio(port_cfg)

    top_n = int(cfg.get("top_n", 4))
    for t in times:
        md = safe_build_md_slice(dfs, t)

        # manage
        for pos in list(pf.positions):
            row = md.get(pos.symbol)
            if row is None:
                continue
            adj = strat.manage_position(t, pos.symbol, pos, row, ctx={"portfolio": pf})
            if adj.action == "EXIT":
                pf.close(pos, t, row["close"], reason=adj.reason)
            elif adj.action == "MOVE_SL" and adj.new_stop is not None:
                pos.stop_price = adj.new_stop
            elif adj.action == "MOVE_TP" and adj.new_tp is not None:
                pos.take_profit = adj.new_tp

        # entries
        uni = strat.universe(t, md)
        ranked = strat.rank(t, md, uni)[:top_n]
        for sym in ranked:
            row = md.get(sym)
            if row is None:
                continue
            sig = strat.entry_signal(t, sym, row, ctx={"portfolio": pf})
            if sig is None:
                continue
            if not pf.can_open(port_cfg):
                continue
            pf.open(symbol=sym, signal=sig, t=t, last_price=row["close"])

    os.makedirs(results_dir, exist_ok=True)
    trades_csv = os.path.join(results_dir, "trades.csv")
    summary_csv = os.path.join(results_dir, "summary.csv")
    pf.save_trades(trades_csv)
    pf.save_summary(summary_csv)
    print(f"[paper-db] saved {trades_csv} and {summary_csv}")

# ---------- PAPER-API runner (live prices, simulated fills, full logging) ----------
def run_paper_api(cfg: dict, args):
    assert EnginePortfolio is not None, "engine.portfolio.Portfolio unavailable"

    strat_path = cfg.get("strategy_class", "strategies.cross_sectional_rs.CrossSectionalRS")
    strat = load_strategy(strat_path, cfg)

    port_cfg = {
        "initial_equity": float(cfg.get("initial_equity", cfg.get("start_cash", 200.0))),
        "fee_rate": float(cfg.get("fee_rate", 0.0006)),
        "slippage_per_side": float(cfg.get("slippage_per_side", cfg.get("slip_bps", 1.5) / 10000.0
                                           if isinstance(cfg.get("slip_bps", 0), (int, float)) else 0.0003)),
        "tick_pct": float(cfg.get("tick_pct", 0.0001)),
        "position_notional": float(cfg.get("notional", 2.2)),
        "max_notional_frac": float(cfg.get("max_notional_frac", 0.5)),
        "funding_rate_hour": float(cfg.get("funding_rate_hour", 0.0)),
    }
    pf = EnginePortfolio(port_cfg)

    use_mock = bool(getattr(args, "dry_run", False)) or (ccxt is None)
    fetcher = MockFetcher() if use_mock else CCXTFetcher(exchange=args.exchange, symbol_format=args.symbol_format, debug=args.debug)

    os.makedirs(args.results_dir, exist_ok=True)
    orders_db = args.orders_db or os.path.join(args.results_dir, "orders.sqlite")
    ensure_orders_db(orders_db)
    session_db_path, cache_out_path = ensure_session_dbs(args.results_dir, args.session_db, args.cache_out)

    run_id = datetime.utcnow().strftime("PA_%Y%m%d_%H%M%S")
    write_config_snapshot(session_db_path, run_id, cfg)

    top_n = int(cfg.get("top_n", 4))
    tf = str(cfg.get("timeframe", "1h"))
    tf_sec = parse_timeframe_to_seconds(tf)
    print(f"[paper-api] polling every {args.poll_sec}s; entries at bar close +{args.bar_delay_sec}s; orders -> {orders_db}")
    last_bar_ts = None
    iters_left = getattr(args, "iterations", None) if getattr(args, "dry_run", False) else None

    while True:
        now = datetime.utcnow().replace(tzinfo=timezone.utc)
        bar_close = round_bar_close(now, tf)
        if (last_bar_ts is None or bar_close > last_bar_ts) and (now - bar_close).total_seconds() >= args.bar_delay_sec:
            last_bar_ts = bar_close

            # Build real-time md (with hour-cache support)
            universe = sorted(set(fetcher.by_base.values()))
            md = {}
            for ccxt_sym in universe:
                feats = {}
                if args.hour_cache == "load":
                    feats = read_hour_cache_row(cache_out_path, ccxt_sym, bar_close)
                if not feats:
                    df = fetcher.fetch_ohlcv_df(ccxt_sym, timeframe=tf, limit=max(60, args.limit_klines))
                    if df is None or len(df) < 30:
                        continue
                    feats_df = compute_feats(df, tf_seconds=tf_sec)
                    if args.hour_cache in ("save", "load"):
                        try: cache_out_upsert(cache_out_path, ccxt_sym, feats_df)
                        except Exception: pass
                    feats = feats_df.iloc[-1].to_dict()
                md[ccxt_sym] = feats
                print(".", end="", flush=True)

            # exits
            for pos in list(pf.positions):
                row = md.get(pos.symbol)
                if row is None:
                    continue
                adj = strat.manage_position(bar_close, pos.symbol, pos, row, ctx={"portfolio": pf})
                if adj.action == "EXIT":
                    px = float(row.get("close") or 0.0) * (1 - port_cfg["slippage_per_side"])  # assume sell
                    pf.close(pos, bar_close, px, reason=adj.reason)
                    insert_order_row(orders_db, {
                        "order_id": str(uuid.uuid4()),
                        "ts_utc": datetime.utcnow().isoformat(),
                        "bar_time_utc": bar_close.isoformat(),
                        "mode": "paper_api",
                        "symbol": pos.symbol,
                        "side": "sell",
                        "type": "market",
                        "price": float(px),
                        "qty": float(pos.qty),
                        "status": "filled",
                        "reason": adj.reason or "exit",
                        "run_id": run_id,
                        "extra": json.dumps({"sim": True})
                    })

            # entries + decisions logging
            uni = strat.universe(bar_close, md)
            ranked = strat.rank(bar_close, md, uni)[:top_n]
            selected_syms = list(ranked)
            write_decisions(session_db_path, run_id, bar_close, ranked, selected_syms)
            opened = 0

            for sym in ranked:
                row = md.get(sym)
                if row is None:
                    continue
                sig = strat.entry_signal(bar_close, sym, row, ctx={"portfolio": pf})
                if sig is None:
                    continue
                if not pf.can_open(port_cfg):
                    continue
                entry_px = float(row.get("close") or 0.0) * (1 + port_cfg["slippage_per_side"])  # assume buy
                pos = pf.open(symbol=sym, signal=sig, t=bar_close, last_price=entry_px)
                opened += 1
                insert_order_row(orders_db, {
                    "order_id": str(uuid.uuid4()),
                    "ts_utc": datetime.utcnow().isoformat(),
                    "bar_time_utc": bar_close.isoformat(),
                    "mode": "paper_api",
                    "symbol": sym,
                    "side": "buy",
                    "type": "market",
                    "price": float(entry_px),
                    "qty": float(getattr(pos, "qty", 0.0)),
                    "status": "filled",
                    "reason": "entry",
                    "run_id": run_id,
                    "extra": json.dumps({"sim": True})
                })

            # equity snapshot + save trades & summary each bar
            eq = {
                "equity": getattr(pf, "equity", 0.0),
                "cash": getattr(pf, "cash", 0.0),
                "position_value": getattr(pf, "position_value", 0.0),
                "realized_pnl_cum": getattr(pf, "realized_pnl_cum", 0.0),
                "unrealized_pnl": getattr(pf, "unrealized_pnl", 0.0)
            }
            write_equity(session_db_path, run_id, bar_close, eq)

            trades_csv = os.path.join(args.results_dir, "trades.csv")
            summary_csv = os.path.join(args.results_dir, "summary.csv")
            pf.save_trades(trades_csv)
            pf.save_summary(summary_csv)

            print(f"\n[paper-api] bar {bar_close.isoformat()} processed: positions={len(pf.positions)}")
            if args.heat_report and opened == 0:
                _print_heat_from_strategy(strat, "paper-api", bar_close, md, uni)

            if iters_left is not None:
                iters_left -= 1
                if iters_left <= 0:
                    break
        else:
            print(".", end="", flush=True)  # heartbeat dot
        time.sleep(args.poll_sec)

# ---------- LIVE runner (BingX via CCXT; simplified bracket flow) ----------
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

def run_live(cfg: dict, args):
    assert ccxt is not None, "ccxt required for LIVE mode"
    strat_path = cfg.get("strategy_class", "strategies.cross_sectional_rs.CrossSectionalRS")
    strat = load_strategy(strat_path, cfg)

    # Auth (.env support)
    if args.env_file and os.path.exists(args.env_file):
        for line in open(args.env_file, "r", encoding="utf-8").read().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip()

    api_k = os.environ.get("BINGX_KEY", "")
    api_s = os.environ.get("BINGX_SECRET", "")
    print(f'[LIVE API] key="{mask(api_k)}", secret="{mask(api_s)}"')
    fetcher = CCXTFetcher(exchange=args.exchange, symbol_format=args.symbol_format, debug=args.debug)

    top_n = int(cfg.get("top_n", 4))
    notional = float(cfg.get("notional", 2.2))
    position_mode = cfg.get("position_mode", "hedge")
    tf = str(cfg.get("timeframe", "1h"))
    tf_sec = parse_timeframe_to_seconds(tf)

    os.makedirs(args.results_dir, exist_ok=True)
    # LIVE session DB & cache setup
    session_db_path, cache_out_path = ensure_session_dbs(args.results_dir, args.session_db, args.cache_out)

    run_id = datetime.utcnow().strftime("LIVE_%Y%m%d_%H%M%S")
    write_config_snapshot(session_db_path, run_id, cfg)

    state_path = os.path.join(args.results_dir, "live_state.json")
    state = {}
    if os.path.exists(state_path):
        try:
            state = json.load(open(state_path, "r", encoding="utf-8"))
        except Exception:
            state = {}
    state.setdefault("positions", {})

    last_bar_ts = None
    print(f"[live] polling every {args.poll_sec}s; entries at bar close +{args.bar_delay_sec}s")
    while True:
        now = datetime.utcnow().replace(tzinfo=timezone.utc)
        bar_close = round_bar_close(now, tf)
        if (last_bar_ts is None or bar_close > last_bar_ts) and (now - bar_close).total_seconds() >= args.bar_delay_sec:
            last_bar_ts = bar_close
            # md
            universe = sorted(set(fetcher.by_base.values()))
            md = {}
            for ccxt_sym in universe:
                feats = {}
                if args.hour_cache == "load":
                    feats = read_hour_cache_row(cache_out_path, ccxt_sym, bar_close)
                if not feats:
                    df = fetcher.fetch_ohlcv_df(ccxt_sym, timeframe=tf, limit=max(60, args.limit_klines))
                    if df is None or len(df) < 30:
                        continue
                    feats_df = compute_feats(df, tf_seconds=tf_sec)
                    if args.hour_cache in ("save","load"):
                        try: cache_out_upsert(cache_out_path, ccxt_sym, feats_df)
                        except Exception: pass
                    feats = feats_df.iloc[-1].to_dict()
                md[ccxt_sym] = feats
                print(".", end="", flush=True)

            # pipeline
            uni = strat.universe(bar_close, md)
            ranked = strat.rank(bar_close, md, uni)[:top_n]
            opened = 0
            selected_syms = list(ranked)
            write_decisions(session_db_path, run_id, bar_close, ranked, selected_syms)
            opened = 0
            # (equity snapshot for bookkeeping; LIVE equity tracked externally)
            write_equity(session_db_path, run_id, bar_close, {
                "equity": 0.0, "cash": 0.0, "position_value": 0.0,
                "realized_pnl_cum": 0.0, "unrealized_pnl": 0.0
            })
            for sym in ranked:
                row = md.get(sym)
                if row is None:
                    continue
                sig = strat.entry_signal(bar_close, sym, row, ctx={})
                if sig is None or getattr(sig, "side", "LONG") != "LONG":
                    continue
                entry_px = fetcher.fetch_ticker_price(sym) or float(row.get("close") or 0.0)
                if not entry_px:
                    continue
                res = place_open_long(fetcher, sym, notional, entry_px, position_mode)
                if not res.get("ok"):
                    print(f"[open FAIL] {sym}: {res}", file=sys.stderr)
                    continue
                qty = float(res["qty"])
                # log order to DB and print
                insert_order_row(session_db_path, {
                    "order_id": f"LIVE-{uuid.uuid4()}",
                    "ts_utc": datetime.utcnow().isoformat(),
                    "bar_time_utc": bar_close.isoformat(),
                    "mode": "live",
                    "symbol": sym,
                    "side": "buy",
                    "type": "market",
                    "price": float(entry_px),
                    "qty": float(qty),
                    "status": "filled",
                    "reason": "entry",
                    "run_id": run_id,
                    "extra": json.dumps({"notional": notional}) 
                })
                print(f"[open OK] {sym} qty={qty:.6g} px={entry_px}", flush=True)
                state["positions"][sym] = {"entry": entry_px, "qty": qty, "ts": bar_close.isoformat()}
                json.dump(state, open(state_path, "w", encoding="utf-8"), indent=2)
                opened += 1
            if args.heat_report and opened == 0:
                _print_heat_from_strategy(strat, "live", bar_close, md, uni)
            print(f"[live] opened={opened} at {bar_close.isoformat()}")
        else:
            print(".", end="", flush=True)  # heartbeat dot
        time.sleep(args.poll_sec)

# ---------- BACKTEST delegator ----------
def run_backtest(cfg_path: str, limit_bars: Optional[int] = None):
    bt_entry = os.path.join(os.getcwd(), "backtester_core.py")
    if not os.path.exists(bt_entry):
        raise SystemExit("backtester_core.py not found. Run from the backtester repo root.")
    cmd = [sys.executable, bt_entry, "--cfg", cfg_path]
    if limit_bars:
        cmd += ["--limit-bars", str(int(limit_bars))]
    print("[backtest] exec:", " ".join(cmd))
    os.execvp(sys.executable, cmd)

# ---------- CLI ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["backtest", "paper", "live"], required=True)
    ap.add_argument("--paper-source", choices=["db", "api"], default="db")
    ap.add_argument("--orders-db", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--iterations", type=int, default=1)

    ap.add_argument("--cfg", required=True, help="YAML/JSON config with strategy_class and params")
    ap.add_argument("--db", help="Cache DB path for PAPER (db)")
    ap.add_argument("--results-dir", default="runner_results")
    ap.add_argument("--limit-bars", type=int, default=0)

    # Session & cache-out
    ap.add_argument("--session-db", default="")
    ap.add_argument("--cache-out", default="")
    ap.add_argument("--hour-cache", choices=["off","save","load"], default="off",
                    help="Current-bar cache: save (write) or load (read) features from cache_out for speed-up")

    # LIVE / PAPER-API params
    ap.add_argument("--env-file", default="")
    ap.add_argument("--exchange", default="bingx")
    ap.add_argument("--symbol-format", default="usdtm")
    ap.add_argument("--poll-sec", type=int, default=15)
    ap.add_argument("--bar-delay-sec", type=int, default=10)
    ap.add_argument("--limit_klines", type=int, default=180)
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--heat-report", action="store_true", default=False,
                    help="Print nearest-to-entry (heat) using strategy thresholds when no entries open on a bar.")

    args = ap.parse_args()
    cfg = load_yaml_or_json(args.cfg)

    if args.mode == "backtest":
        return run_backtest(args.cfg, args.limit_bars if args.limit_bars > 0 else None)

    if args.mode == "paper":
        if args.paper_source == "api":
            return run_paper_api(cfg, args)
        else:
            if not args.db:
                raise SystemExit("--db is required for PAPER mode when --paper-source=db")
            return run_paper(args.db, cfg, args.results_dir, args.limit_bars if args.limit_bars > 0 else None)

    # LIVE
    return run_live(cfg, args)

if __name__ == "__main__":
    main()
