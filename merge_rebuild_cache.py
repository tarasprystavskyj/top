#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Merge multiple cache DBs (SQLite) containing table 'price_indicators' into one,
deduplicate by (symbol, datetime_utc), and recompute features so windows scale with TF.
Output schema stays compatible with fetch_build_cache_v14.py.

Usage:
  python3 merge_rebuild_cache.py -o merged.db db1.db db2.db db3.db
Options:
  --timeframe auto|5m|15m|1h|4h|1d  (override TF for ALL symbols; default=auto detect per symbol)
  --chunksize  50000   (batch-commit rows to reduce memory)
"""
from __future__ import annotations

import argparse, sqlite3, sys
from pathlib import Path
import pandas as pd, numpy as np

def log(*a): print(*a, file=sys.stderr)

def detect_tf_seconds(dts: pd.Series) -> int:
    d = dts.sort_values().diff().dropna().dt.total_seconds().values
    if len(d)==0: return 3600
    cands=[60,180,300,600,900,1800,3600,7200,14400,21600,43200,86400]
    med=float(np.median(d)); return int(min(cands, key=lambda x: abs(x-med)))

def tf_to_seconds(tf: str) -> int:
    tf=tf.strip().lower()
    if tf=="auto": return -1
    units={"m":60,"h":3600,"d":86400,"w":604800}
    try: return int(tf[:-1])*units[tf[-1]]
    except Exception: return 3600

def ensure_schema(db_path: str):
    con=sqlite3.connect(db_path); cur=con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS price_indicators(
        symbol TEXT, datetime_utc TEXT,
        open REAL, high REAL, low REAL, close REAL, volume REAL,
        rsi REAL, stochastic REAL, mfi REAL, overbought_index REAL,
        atr_ratio REAL, gain_24h_before REAL,
        dp6h REAL, dp12h REAL,
        quote_volume REAL, qv_24h REAL, vol_surge_mult REAL,
        PRIMARY KEY(symbol, datetime_utc)
    )""")
    con.commit(); con.close()

def compute_feats(df: pd.DataFrame, tf_seconds: int) -> pd.DataFrame:
    g=df.sort_values("datetime_utc").copy()
    prev=g["close"].shift(1)
    tr=pd.concat([(g["high"]-g["low"]).abs(),(g["high"]-prev).abs(),(g["low"]-prev).abs()],axis=1).max(axis=1)
    atr=tr.ewm(alpha=1/14.0, adjust=False).mean()
    g["atr_ratio"]=(atr/g["close"]).replace([np.inf,-np.inf],np.nan).fillna(0.0)
    g["quote_volume"]=(g["volume"]*g["close"]).replace([np.inf,-np.inf],np.nan).fillna(0.0)
    bars_24=max(1,int(round(24*3600/max(1,tf_seconds))))
    bars_6 =max(1,int(round( 6*3600/max(1,tf_seconds))))
    bars_12=max(1,int(round(12*3600/max(1,tf_seconds))))
    g["gain_24h_before"]=(g["close"]/g["close"].shift(bars_24)-1.0).fillna(0.0)
    g["dp6h"] =(g["close"]/g["close"].shift(bars_6) -1.0).fillna(0.0)
    g["dp12h"]=(g["close"]/g["close"].shift(bars_12)-1.0).fillna(0.0)
    g["qv_24h"]=g["quote_volume"].rolling(bars_24, min_periods=1).sum()
    avg=g["qv_24h"]/float(bars_24)
    with np.errstate(divide="ignore", invalid="ignore"):
        g["vol_surge_mult"]=np.where(avg>0, g["quote_volume"]/avg, 0.0)
    for k in ("rsi","stochastic","mfi","overbought_index"): g[k]=0.0
    return g

def try_load(db_path: str) -> pd.DataFrame | None:
    try:
        con=sqlite3.connect(db_path)
        df=pd.read_sql_query("SELECT symbol, datetime_utc, open, high, low, close, volume FROM price_indicators", con)
        con.close()
        df["datetime_utc"]=pd.to_datetime(df["datetime_utc"], utc=True, errors="coerce")
        df=df.dropna(subset=["datetime_utc"]); df["src"]=Path(db_path).name
        log(f"[OK] read {db_path} rows={len(df)}")
        return df
    except Exception as e:
        log(f"[SKIP] {db_path}: {e}")
        return None

def write_chunked(db_path: str, df: pd.DataFrame, chunksize: int = 50000):
    con=sqlite3.connect(db_path); cur=con.cursor()
    cols = ["symbol","datetime_utc","open","high","low","close","volume",
            "rsi","stochastic","mfi","overbought_index","atr_ratio","gain_24h_before",
            "dp6h","dp12h","quote_volume","qv_24h","vol_surge_mult"]
    tpl = ",".join(["?"]*len(cols))
    sql=f"INSERT OR REPLACE INTO price_indicators({','.join(cols)}) VALUES({tpl})"
    n=len(df); i=0
    while i<n:
        part=df.iloc[i:min(i+chunksize,n)]
        rows=[(
            str(r["symbol"]), pd.to_datetime(r["datetime_utc"], utc=True).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), float(r["volume"]),
            float(r["rsi"]), float(r["stochastic"]), float(r["mfi"]), float(r["overbought_index"]),
            float(r["atr_ratio"]), float(r["gain_24h_before"]), float(r["dp6h"]), float(r["dp12h"]),
            float(r["quote_volume"]), float(r["qv_24h"]), float(r["vol_surge_mult"]),
        ) for _,r in part.iterrows()]
        cur.executemany(sql, rows); con.commit()
        log(f"[WRITE] {i+len(rows)}/{n}")
        i += len(rows)
    con.close()

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("-o","--output", required=True)
    ap.add_argument("--timeframe", default="auto")
    ap.add_argument("--chunksize", type=int, default=50000)
    ap.add_argument("inputs", nargs="+")
    args=ap.parse_args()

    frames=[df for p in args.inputs if Path(p).exists() and (df:=try_load(p)) is not None]
    if not frames: raise SystemExit("No inputs readable.")
    raw=pd.concat(frames, ignore_index=True).sort_values(["symbol","datetime_utc","src"])

    # dedupe by (symbol, datetime_utc)
    merged=raw.drop_duplicates(subset=["symbol","datetime_utc"], keep="last").copy()

    tf_override=tf_to_seconds(args.timeframe)
    enriched=[]
    for sym, sub in merged.groupby("symbol", sort=False):
        tf_sec = detect_tf_seconds(sub["datetime_utc"]) if tf_override<0 else tf_override
        feats = compute_feats(sub[["symbol","datetime_utc","open","high","low","close","volume"]], tf_sec)
        enriched.append(feats)
    out=pd.concat(enriched, ignore_index=True).sort_values(["datetime_utc","symbol"])

    ensure_schema(args.output)
    write_chunked(args.output, out, chunksize=args.chunksize)
    log(f"[DONE] merged rows={len(out)} -> {args.output}")

if __name__=="__main__":
    main()
