#!/usr/bin/env python3
import argparse, sqlite3, numpy as np, pandas as pd, os

def main():
    ap = argparse.ArgumentParser(description='Build compact NPZ cache for fast dual backtester/tuner')
    ap.add_argument('--db', required=True)
    ap.add_argument('--symbol', default='')
    ap.add_argument('--out', required=True)
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    q = 'SELECT symbol, datetime_utc, close FROM price_indicators'
    params = []
    if args.symbol:
        q += ' WHERE symbol = ?'
        params.append(args.symbol)
    q += ' ORDER BY datetime_utc ASC, symbol ASC'
    df = pd.read_sql_query(q, con, params=params)
    if df.empty:
        raise SystemExit('No rows found')
    if not args.symbol:
        syms = sorted(df['symbol'].unique())
        if len(syms) != 1:
            raise SystemExit(f'Multiple symbols found: {syms}. Pass --symbol explicitly.')
    symbol = str(df['symbol'].iloc[0])
    ts = pd.to_datetime(df['datetime_utc'], utc=True).astype('int64').to_numpy() // 1_000_000_000
    close = df['close'].astype('float64').to_numpy()
    np.savez_compressed(args.out, symbol=symbol, timestamp_s=ts.astype('int64'), close=close)
    print(f'[ok] wrote {args.out} rows={len(close)} symbol={symbol} range={df.datetime_utc.iloc[0]}..{df.datetime_utc.iloc[-1]}')

if __name__ == '__main__':
    main()
