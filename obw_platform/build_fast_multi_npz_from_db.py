#!/usr/bin/env python3
from __future__ import annotations
import argparse
import sqlite3
from pathlib import Path
import numpy as np
import pandas as pd

DEFAULT_OPTIONAL_COLUMNS = [
    'trend_ma', 'trend_ma_prev', 'trend_slope_pct',
    'trend_target_pct_long', 'trend_target_pct_short',
]


def existing_columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in con.execute(f'PRAGMA table_info({table})')]


def main():
    ap = argparse.ArgumentParser(description='Build multi-symbol fast NPZ from standard SQLite price_indicators DB')
    ap.add_argument('--db', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--symbols-file', default='')
    ap.add_argument('--include-optional', action='store_true', help='Also export known optional cached feature columns when present')
    ap.add_argument('--debug', action='store_true', help='Verbose progress output')
    args = ap.parse_args()

    if args.debug:
        print(f'[cfg] db={args.db} out={args.out} include_optional={args.include_optional}', flush=True)
    con = sqlite3.connect(args.db)
    cols = existing_columns(con, 'price_indicators')
    select_cols = ['symbol', 'datetime_utc', 'open', 'high', 'low', 'close', 'volume']
    if args.include_optional:
        select_cols += [c for c in DEFAULT_OPTIONAL_COLUMNS if c in cols]

    q = 'SELECT ' + ', '.join(select_cols) + ' FROM price_indicators'
    params = []
    if args.symbols_file:
        syms = [line.strip() for line in open(args.symbols_file, 'r', encoding='utf-8') if line.strip() and not line.startswith('#')]
        if syms:
            q += ' WHERE symbol IN (%s)' % ','.join(['?'] * len(syms))
            params.extend(syms)
    q += ' ORDER BY symbol ASC, datetime_utc ASC'
    df = pd.read_sql_query(q, con, params=params)
    con.close()
    if df.empty:
        raise SystemExit('No rows found')

    symbols = []
    offsets = [0]
    out_parts: dict[str, list[np.ndarray]] = {'timestamp_s': []}
    num_cols = [c for c in df.columns if c not in {'symbol', 'datetime_utc'}]

    pos = 0
    for sym, part in df.groupby('symbol', sort=True):
        symbols.append(sym)
        ts = pd.to_datetime(part['datetime_utc'], utc=True).astype('int64').to_numpy() // 1_000_000_000
        out_parts['timestamp_s'].append(ts.astype(np.int64))
        for col in num_cols:
            out_parts.setdefault(col, []).append(part[col].astype('float64').to_numpy())
        pos += len(part)
        offsets.append(pos)
        if args.debug:
            print(f'[sym] {sym} rows={len(part)}', flush=True)

    out = {
        'symbols': np.asarray(symbols, dtype=object),
        'offsets': np.asarray(offsets, dtype=np.int64),
    }
    for col, arrs in out_parts.items():
        out[col] = np.concatenate(arrs) if arrs else np.asarray([], dtype=np.float64)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(args.out, **out)
    print(f'[ok] wrote {args.out} symbols={len(symbols)} rows={pos}', flush=True)


if __name__ == '__main__':
    main()
