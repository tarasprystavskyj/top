#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_build_cache_v12.py

One-shot pipeline: fetch BingX futures klines for a universe and build a SQLite
indicator cache usable by both TG v12 (live/paper) and OBW backtests.

Usage examples:
  # recent N bars (per symbol) and write DB from scratch
  python3 fetch_build_cache_v12.py -i universe_symbols_bingx.csv -t 1h --limit 1440 -o combined_cache_1440.db --fresh

  # bounded date range (UTC ISO) with API key header
  python3 fetch_build_cache_v12.py -i universe_symbols_bingx.csv -t 1h --start-date 2025-06-01 --end-date 2025-08-13 -o combined_cache_range.db --api-key $BINGX_KEY

Notes:
- Interval strings are BingX/ccxt-like (e.g., 1h, 4h). This script expects HOUR granularity for
  dp6h/dp12h/qv_24h definitions.
- If DB already exists, use --fresh to recreate it; otherwise rows are upserted.
"""
import os, sys, time, math, argparse, sqlite3, glob
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple

import requests
import pandas as pd
import numpy as np

API_BASE = "https://open-api.bingx.com"
MAX_LIMIT_PER_CALL = 200
REQUEST_DELAY = float(os.getenv("BINGX_REQUEST_DELAY","0.25"))

# ---------------- CLI ----------------
def parse_args():
    p = argparse.ArgumentParser(description="Fetch BingX klines and build indicator cache DB")
    p.add_argument("-i","--input", required=True, help="CSV with 'symbol' column (exchange symbols, e.g., BTC-USDT or BTC/USDT:USDT)")
    p.add_argument("-t","--interval", default="1h", help="Kline interval (e.g., 1h, 4h)")
    p.add_argument("--start-date", help="Start date inclusive (ISO, e.g., 2025-07-01 or 2025-07-01T00:00:00)")
    p.add_argument("--end-date",   help="End date inclusive (ISO), default now (UTC)")
    p.add_argument("--limit", type=int, default=500, help="Recent candles to fetch if no date range given")
    p.add_argument("-o","--out-db", default="combined_cache.db", help="SQLite DB path to write/extend")
    p.add_argument("--fresh", action="store_true", help="Recreate DB from scratch")
    p.add_argument("--api-key", default=None, help="Optional BingX API key header")
    p.add_argument("--write-csv", action="store_true", help="Also dump per-symbol *_history.csv files next to DB")
    return p.parse_args()

def to_millis(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)

def _headers(api_key: Optional[str] = None) -> dict:
    return {"X-BX-APIKEY": api_key} if api_key else {}

def fetch_klines(symbol: str, interval: str, limit: int, start_ms: Optional[int]=None, end_ms: Optional[int]=None, api_key: Optional[str]=None):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    if start_ms is not None: params["startTime"] = start_ms
    if end_ms   is not None: params["endTime"]   = end_ms
    try:
        r = requests.get(f"{API_BASE}/openApi/swap/v2/quote/klines", params=params, headers=_headers(api_key), timeout=15)
        r.raise_for_status()
        data = r.json()
        payload = data.get("data") if isinstance(data, dict) and "data" in data else data
        if not isinstance(payload, list):
            print(f"[WARN] {symbol} unexpected payload: {payload}")
            return []
        return payload
    except Exception as e:
        print(f"[ERR] fetch_klines {symbol}: {e}")
        return []

def sliding_fetch(symbol: str, interval: str, start_dt: datetime, end_dt: datetime, api_key: Optional[str]) -> list:
    out = []
    s = to_millis(start_dt); end = to_millis(end_dt)
    while s < end:
        chunk = fetch_klines(symbol, interval, MAX_LIMIT_PER_CALL, start_ms=s, end_ms=end, api_key=api_key)
        if not chunk: break
        out.extend(chunk)
        last_ts = chunk[-1].get("time") or 0
        s = (last_ts + 1) if last_ts else end
        time.sleep(REQUEST_DELAY)
        if len(chunk) < MAX_LIMIT_PER_CALL: break
    return out

def fetch_recent(symbol: str, interval: str, limit: int, api_key: Optional[str]) -> list:
    return fetch_klines(symbol, interval, limit, api_key=api_key)

def normalize_klines(raw: list) -> pd.DataFrame:
    rows = []
    for e in raw:
        if not isinstance(e, dict): continue
        try:
            ts_ms = int(e.get("time"))
            dt = datetime.utcfromtimestamp(ts_ms/1000.0)
            rows.append({
                "datetime_utc": pd.Timestamp(dt).strftime("%Y-%m-%d %H:%M:%S"),
                "open": float(e.get("open", 0)),
                "high": float(e.get("high", 0)),
                "low": float(e.get("low", 0)),
                "close": float(e.get("close", 0)),
                "volume": float(e.get("volume", 0)),
            })
        except Exception:
            continue
    return pd.DataFrame(rows)

# --------------- indicators (match v12 bot) ---------------
def rsi_series(close: pd.Series, period: int = 14) -> pd.Series:
    # mirrored from v12 bot (EMA-based RSI)
    delta = close.diff()
    up = np.where(delta > 0, delta, 0.0)
    down = np.where(delta < 0, -delta, 0.0)
    roll_up = pd.Series(up, index=close.index).ewm(alpha=1/period, adjust=False).mean()
    roll_down = pd.Series(down, index=close.index).ewm(alpha=1/period, adjust=False).mean()
    rs = roll_up / roll_down.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return pd.Series(rsi, index=close.index).fillna(50.0)

def stoch_k(df: pd.DataFrame, length: int = 14, smooth: int = 3) -> pd.Series:
    low_min = df["low"].rolling(length, min_periods=1).min()
    high_max = df["high"].rolling(length, min_periods=1).max()
    k = 100 * (df["close"] - low_min) / (high_max - low_min).replace(0, np.nan)
    return k.rolling(smooth, min_periods=1).mean().fillna(50.0)

def mfi_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    rmf = tp * df["volume"]
    sign = np.sign(tp.diff().fillna(0))
    pos = (rmf * (sign >= 0)).rolling(period, min_periods=1).sum()
    neg = (rmf * (sign <  0)).rolling(period, min_periods=1).sum().replace(0, np.nan)
    mfi = 100 - (100 / (1 + (pos / neg)))
    return mfi.fillna(50.0)

def compute_feats(df: pd.DataFrame) -> pd.DataFrame:
    # mirrored from v12 bot compute_feats
    o = df["open"].astype(float).values
    h = df["high"].astype(float).values
    l = df["low"].astype(float).values
    c = df["close"].astype(float).values
    v = df["volume"].astype(float).values
    n = len(df)
    if n < 30:
        return pd.DataFrame(index=df.index)

    # ATR% (14)
    tr = np.zeros(n); tr[0] = h[0]-l[0]
    for i in range(1,n):
        tr[i] = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
    atr = pd.Series(tr).rolling(14, min_periods=1).mean().values
    atr_ratio = atr / np.maximum(c, 1e-12)

    # Momentum (dp6h/dp12h)
    def pct(a,b): return (c[b]-c[a]) / max(c[a],1e-12)
    dp6 = np.zeros(n); dp12 = np.zeros(n)
    for i in range(n):
        a6 = max(0, i-6); a12 = max(0, i-12)
        dp6[i] = pct(a6, i); dp12[i] = pct(a12, i)

    # Liquidity & surge
    qv_bar = c * v
    qv_24h = pd.Series(qv_bar).rolling(24, min_periods=1).sum().values
    avg_24 = pd.Series(qv_bar).rolling(24, min_periods=1).mean().values
    vol_surge_mult = np.where(avg_24>0, qv_bar/avg_24, 0.0)

    # Oscillators
    rsi = rsi_series(df["close"])
    st_k = stoch_k(df)
    mfi = mfi_series(df)

    # High-close proximity (hcp) for optional cached OB index
    hcp = 100.0 * np.where(h>0, np.minimum(1.0, c/np.maximum(h,1e-12)), 0.0)

    out = pd.DataFrame({
        "open": df["open"].values, "high": df["high"].values, "low": df["low"].values, "close": df["close"].values, "volume": df["volume"].values,
        "rsi": rsi.values, "stoch_k": st_k.values, "mfi": mfi.values,
        "atr_ratio": atr_ratio, "dp6h": dp6, "dp12h": dp12, "mom": dp6+dp12,
        "quote_volume": qv_bar, "qv_24h": qv_24h, "vol_surge_mult": vol_surge_mult,
        "hcp": hcp
    }, index=df.index)
    return out

def weighted_ob(row, w_rsi=0.4, w_stoch=0.2, w_mfi=0.2, w_hcp=0.2) -> float:
    r = max(0.0, min(100.0, float(row.get("rsi", 50.0))))
    s = max(0.0, min(100.0, float(row.get("stoch_k", 50.0))))
    m = max(0.0, min(100.0, float(row.get("mfi", 50.0))))
    h = float(row.get("hcp", 0.0))
    return w_rsi*r + w_stoch*s + w_mfi*m + w_hcp*h

# --------------- DB ---------------
def ensure_columns(con: sqlite3.Connection):
    # Add missing columns to price_indicators if coming from an older schema
    cur = con.cursor()
    cur.execute("PRAGMA table_info(price_indicators)")
    cols = {r[1] for r in cur.fetchall()}
    needed = [
        "stoch_k","dp6h","dp12h","mom",
        "quote_volume","qv_24h","vol_surge_mult","hcp","overbought_index"
    ]
    for c in needed:
        if c not in cols:
            cur.execute(f"ALTER TABLE price_indicators ADD COLUMN {c} REAL")
    con.commit()

SCHEMA_SQL = '''
CREATE TABLE IF NOT EXISTS price_indicators (
    symbol TEXT NOT NULL,
    datetime_utc TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    rsi REAL, stoch_k REAL, mfi REAL,
    atr_ratio REAL,
    dp6h REAL, dp12h REAL, mom REAL,
    quote_volume REAL, qv_24h REAL, vol_surge_mult REAL,
    hcp REAL,
    overbought_index REAL,
    PRIMARY KEY(symbol, datetime_utc)
);
CREATE INDEX IF NOT EXISTS idx_symbol_time ON price_indicators(symbol, datetime_utc);
'''

def setup_db(db_path: str, fresh: bool=False) -> sqlite3.Connection:
    if fresh and os.path.exists(db_path):
        os.remove(db_path)
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    for stmt in [s.strip() for s in SCHEMA_SQL.split(";") if s.strip()]:
        cur.execute(stmt)
    con.commit()
    ensure_columns(con)
    return con

def upsert_symbol_df(con: sqlite3.Connection, sym: str, feats: pd.DataFrame):
    if feats is None or feats.empty: return 0
    cur = con.cursor()
    to_ins = []
    for ts, row in feats.iterrows():
        ob = weighted_ob(row)  # cached OB with default weights for backtests
        to_ins.append((
            sym,
            pd.Timestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
            float(row.get("open", np.nan)),
            float(row.get("high", np.nan)),
            float(row.get("low", np.nan)),
            float(row.get("close", np.nan)),
            float(row.get("volume", np.nan)),
            float(row.get("rsi", np.nan)),
            float(row.get("stoch_k", np.nan)),
            float(row.get("mfi", np.nan)),
            float(row.get("atr_ratio", np.nan)),
            float(row.get("dp6h", np.nan)),
            float(row.get("dp12h", np.nan)),
            float(row.get("mom", np.nan)),
            float(row.get("quote_volume", np.nan)),
            float(row.get("qv_24h", np.nan)),
            float(row.get("vol_surge_mult", np.nan)),
            float(row.get("hcp", np.nan)),
            float(ob),
        ))
    cur.executemany("""
        INSERT OR REPLACE INTO price_indicators (
            symbol, datetime_utc, open, high, low, close, volume,
            rsi, stoch_k, mfi,
            atr_ratio,
            dp6h, dp12h, mom,
            quote_volume, qv_24h, vol_surge_mult,
            hcp,
            overbought_index
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, to_ins)
    con.commit()
    return len(to_ins)

# --------------- main ---------------
def main():
    args = parse_args()
    df_universe = pd.read_csv(args.input)
    if "symbol" not in df_universe.columns:
        print("Input CSV must have a 'symbol' column.", file=sys.stderr); sys.exit(2)
    symbols = df_universe["symbol"].dropna().astype(str).str.strip().unique().tolist()

    # range vs recent
    if args.start_date:
        start_dt = datetime.fromisoformat(args.start_date)
    else:
        start_dt = None
    end_dt = datetime.fromisoformat(args.end_date) if args.end_date else datetime.utcnow()

    con = setup_db(args.out_db, fresh=args.fresh)

    total_rows = 0
    for i, sym in enumerate(symbols, 1):
        print(f"[{i}/{len(symbols)}] {sym} fetch...")
        if start_dt:
            raw = sliding_fetch(sym, args.interval, start_dt, end_dt, api_key=args.api_key)
        else:
            raw = fetch_recent(sym, args.interval, args.limit, api_key=args.api_key)
        if not raw:
            print(f"[WARN] {sym} empty, skip")
            continue
        df = normalize_klines(raw)
        if df.empty:
            print(f"[WARN] {sym} failed to normalize, skip")
            continue
        # optional CSV dump
        if args.write_csv:
            out_csv = os.path.join(os.path.dirname(args.out_db) or ".", f"{sym.replace('/', '_').replace(':','_')}_history.csv")
            df.to_csv(out_csv, index=False)
        # compute feats and upsert
        df = df.sort_values("datetime_utc")
        df.set_index(pd.to_datetime(df["datetime_utc"], utc=True), inplace=True)
        feats = compute_feats(df[["open","high","low","close","volume"]])
        rows = upsert_symbol_df(con, sym, feats)
        total_rows += rows
        print(f"[OK] {sym} rows={rows}")
        time.sleep(REQUEST_DELAY)

    con.close()
    print(f"[DONE] wrote ~{total_rows} rows into {args.out_db}")

if __name__ == "__main__":
    main()
