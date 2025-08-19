#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_build_cache_v13.py
------------------------
Builds/updates a SQLite cache "price_indicators" with required features:
- OHLCV
- atr_ratio (ATR/close, period=14)
- dp6h, dp12h (returns over 6 and 12 bars; for 1h tf => 6h, 12h)
- quote_volume (approx = base_volume * close when exchange doesn't provide quoteVolume)
- qv_24h (rolling sum of 24 bars of quote_volume)
- vol_surge_mult (quote_volume_bar / (qv_24h / 24))

CLI (similar to v12):
  python3 fetch_build_cache_v13.py \
    -i universe_symbols_bingx.csv \
    -t 1h --limit 1440 \
    -o combined_cache_1440.db \
    --fresh

Notes:
- Requires CCXT for live fetching (install on server): pip install ccxt pandas numpy
- Supports BingX (swap) symbols formatted as BASE/USDT:USDT when --ccxt-symbol-format usdtm
- If CCXT returns only base-volume, we multiply by close to estimate quote_volume

CSV format for -i:
  symbol
  BTC
  ETH
  ...

These are BASE tickers; we will map to CCXT market id by --ccxt-symbol-format
"""
import os, sys, argparse, sqlite3, time, math, json
from typing import List, Optional
import pandas as pd
import numpy as np

try:
    import ccxt
except Exception:
    ccxt = None

def map_symbol(base: str, fmt: str="spot", quote="USDT"):
    base = base.strip().upper()
    if fmt == "usdtm":
        # USDT-margined perpetual
        return f"{base}/{quote}:{quote}"
    elif fmt == "spot":
        return f"{base}/{quote}"
    else:
        return base

def calc_atr_ratio(df: pd.DataFrame, period: int = 14) -> pd.Series:
    # True range
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        (df["high"] - df["low"]).abs(),
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs()
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/period, adjust=False).mean()
    return (atr / df["close"]).replace([np.inf, -np.inf], np.nan).fillna(0.0)

def ensure_schema(db_path: str):
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS price_indicators(
        symbol TEXT,
        datetime_utc TEXT,
        open REAL, high REAL, low REAL, close REAL, volume REAL,
        rsi REAL, stochastic REAL, mfi REAL, overbought_index REAL,
        atr_ratio REAL,
        gain_24h_before REAL,
        dp6h REAL, dp12h REAL,
        quote_volume REAL, qv_24h REAL, vol_surge_mult REAL,
        PRIMARY KEY (symbol, datetime_utc)
    )""")
    # Helpful for concurrent readers
    cur.execute("PRAGMA journal_mode=WAL;")
    con.commit(); con.close()

def upsert_rows(db_path: str, rows: List[dict]):
    if not rows: return
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cols = ["symbol","datetime_utc","open","high","low","close","volume",
            "rsi","stochastic","mfi","overbought_index","atr_ratio","gain_24h_before",
            "dp6h","dp12h","quote_volume","qv_24h","vol_surge_mult"]
    placeholders = ",".join(["?"]*len(cols))
    for r in rows:
        vals = [r.get(c) for c in cols]
        cur.execute(f"INSERT INTO price_indicators ({','.join(cols)}) VALUES ({placeholders}) "
                    f"ON CONFLICT(symbol, datetime_utc) DO UPDATE SET "
                    f"{','.join([f'{c}=excluded.{c}' for c in cols[2:]])}", vals)
    con.commit(); con.close()

def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    # df indexed by datetime (utc), columns: open, high, low, close, volume
    out = df.copy()
    # Basic momentum proxies
    out["gain_24h_before"] = (out["close"] / out["close"].shift(24) - 1.0).fillna(0.0)
    out["dp6h"] = (out["close"] / out["close"].shift(6) - 1.0).fillna(0.0)
    out["dp12h"] = (out["close"] / out["close"].shift(12) - 1.0).fillna(0.0)
    # ATR ratio
    out["atr_ratio"] = calc_atr_ratio(out, period=14)
    # Quote volume (approx if missing)
    out["quote_volume"] = (out["volume"] * out["close"]).fillna(0.0)
    # 24h rolling quote volume
    out["qv_24h"] = out["quote_volume"].rolling(24, min_periods=1).sum()
    # Surge multiplier
    avg1 = out["qv_24h"] / 24.0
    with np.errstate(divide='ignore', invalid='ignore'):
        out["vol_surge_mult"] = np.where(avg1>0, out["quote_volume"] / avg1, 0.0)
    # Placeholders for indicators referenced by schema (you can swap in real calcs if needed)
    out["rsi"] = 0.0
    out["stochastic"] = 0.0
    out["mfi"] = 0.0
    out["overbought_index"] = 0.0
    return out

def fetch_ohlcv(exchange, market: str, timeframe: str, limit: int) -> pd.DataFrame:
    # Returns DataFrame with columns open, high, low, close, volume; index as UTC ISO strings
    ohlcv = exchange.fetch_ohlcv(market, timeframe=timeframe, limit=limit)
    if not ohlcv: return pd.DataFrame()
    df = pd.DataFrame(ohlcv, columns=["ts","open","high","low","close","volume"])
    df["datetime_utc"] = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    df = df.set_index("datetime_utc")[["open","high","low","close","volume"]].astype(float)
    return df

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-i","--input-csv", required=True, help="CSV with column 'symbol' (BASE tickers)")
    ap.add_argument("-t","--timeframe", default="1h")
    ap.add_argument("--limit", type=int, default=1440)
    ap.add_argument("-o","--output", required=True, help="Output SQLite DB path")
    ap.add_argument("--exchange", default="bingx")
    ap.add_argument("--ccxt-symbol-format", default="usdtm", choices=["usdtm","spot"])
    ap.add_argument("--fresh", action="store_true", help="Drop & recreate table")
    args = ap.parse_args()

    ensure_schema(args.output)
    if args.fresh and os.path.exists(args.output):
        con = sqlite3.connect(args.output); cur = con.cursor()
        cur.execute("DROP TABLE IF EXISTS price_indicators"); con.commit(); con.close()
        ensure_schema(args.output)

    if ccxt is None:
        print("ERROR: ccxt not installed. Please install on server: pip install ccxt", file=sys.stderr)
        sys.exit(2)

    # Setup exchange
    ex = getattr(ccxt, args.exchange)()
    ex.enableRateLimit = True

    # Load universe
    uni = pd.read_csv(args.input_csv)
    if "symbol" not in uni.columns:
        raise SystemExit("Input CSV must have a 'symbol' column with BASE tickers (e.g., BTC, ETH).")
    bases = [str(x).strip().upper() for x in uni["symbol"].dropna().unique().tolist()]

    for base in bases:
        market = map_symbol(base, fmt=args.ccxt_symbol_format)
        try:
            df = fetch_ohlcv(ex, market, args.timeframe, args.limit)
            if df.empty:
                print(f"[WARN] No OHLCV for {market}"); continue
            feats = compute_features(df)
            rows = []
            for idx, r in feats.iterrows():
                rows.append({
                    "symbol": f"{base}_{args.ccxt_symbol_format}",
                    "datetime_utc": idx,
                    "open": float(r["open"]), "high": float(r["high"]), "low": float(r["low"]), "close": float(r["close"]), "volume": float(r["volume"]),
                    "rsi": float(r["rsi"]), "stochastic": float(r["stochastic"]), "mfi": float(r["mfi"]), "overbought_index": float(r["overbought_index"]),
                    "atr_ratio": float(r["atr_ratio"]), "gain_24h_before": float(r["gain_24h_before"]),
                    "dp6h": float(r["dp6h"]), "dp12h": float(r["dp12h"]),
                    "quote_volume": float(r["quote_volume"]), "qv_24h": float(r["qv_24h"]), "vol_surge_mult": float(r["vol_surge_mult"]),
                })
            upsert_rows(args.output, rows)
            print(f"[OK] {market} rows={len(rows)}")
        except Exception as e:
            print(f"[ERR] {market} {e}", file=sys.stderr)
            continue

if __name__ == "__main__":
    main()
