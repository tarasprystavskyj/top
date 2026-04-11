#!/usr/bin/env python3
from __future__ import annotations
import argparse, sqlite3
from pathlib import Path
import numpy as np
import pandas as pd

def main():
    ap=argparse.ArgumentParser(description='Build multi-symbol fast NPZ from standard SQLite price_indicators DB')
    ap.add_argument('--db', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--symbols-file', default='')
    args=ap.parse_args()
    con=sqlite3.connect(args.db)
    q='SELECT symbol, datetime_utc, close FROM price_indicators'
    params=[]
    if args.symbols_file:
        syms=[line.strip() for line in open(args.symbols_file,'r',encoding='utf-8') if line.strip() and not line.startswith('#')]
        if syms:
            q += ' WHERE symbol IN (%s)' % ','.join(['?']*len(syms))
            params.extend(syms)
    q += ' ORDER BY symbol ASC, datetime_utc ASC'
    df=pd.read_sql_query(q, con, params=params)
    con.close()
    if df.empty:
        raise SystemExit('No rows found')
    symbols=[]; offsets=[]; ts_all=[]; close_all=[]
    pos=0
    for sym, part in df.groupby('symbol', sort=True):
        symbols.append(sym); offsets.append(pos)
        ts = pd.to_datetime(part['datetime_utc'], utc=True).astype('int64').to_numpy() // 1_000_000_000
        cl = part['close'].astype('float64').to_numpy()
        ts_all.append(ts.astype('int64')); close_all.append(cl)
        pos += len(cl)
    np.savez_compressed(args.out,
        symbols=np.asarray(symbols, dtype=object),
        offsets=np.asarray(offsets, dtype=np.int64),
        timestamp_s=np.concatenate(ts_all).astype(np.int64),
        close=np.concatenate(close_all).astype(np.float64),
    )
    print(f'[ok] wrote {args.out} symbols={len(symbols)} rows={pos}')

if __name__=='__main__':
    main()
