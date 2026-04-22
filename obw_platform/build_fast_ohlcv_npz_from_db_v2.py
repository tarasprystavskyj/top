#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, sqlite3
from pathlib import Path
import numpy as np
import pandas as pd

BASE_COLS = {'symbol','datetime_utc','open','high','low','close','volume'}
EXCLUDE_EXTRAS = {'rsi','stochastic','mfi','overbought_index'}

def _table_columns(con, table='price_indicators'):
    return [r[1] for r in con.execute(f'PRAGMA table_info({table})').fetchall()]

def _load_db(db_path: str, symbol: str = '') -> pd.DataFrame:
    con = sqlite3.connect(db_path)
    cols = _table_columns(con)
    if not cols:
        raise SystemExit('price_indicators table not found')
    select_cols = [c for c in cols if c not in EXCLUDE_EXTRAS]
    sql = f"SELECT {','.join(select_cols)} FROM price_indicators"
    params = ()
    if symbol:
        sql += ' WHERE symbol=?'
        params = (symbol,)
    sql += ' ORDER BY symbol ASC, datetime_utc ASC'
    df = pd.read_sql_query(sql, con, params=params)
    con.close()
    if df.empty:
        raise SystemExit('No rows found in price_indicators')
    return df

def _derive_missing_ohlcv(part: pd.DataFrame, mode: str):
    close = part['close'].astype('float64').to_numpy()
    prev_close = np.r_[close[0], close[:-1]]
    if 'open' in part.columns and part['open'].notna().any():
        open_ = part['open'].astype('float64').to_numpy()
    elif mode == 'flat':
        open_ = close.copy()
    else:
        open_ = prev_close.astype('float64')

    if 'high' in part.columns and part['high'].notna().any():
        high = part['high'].astype('float64').to_numpy()
    else:
        high = np.maximum(open_, close).astype('float64')

    if 'low' in part.columns and part['low'].notna().any():
        low = part['low'].astype('float64').to_numpy()
    else:
        low = np.minimum(open_, close).astype('float64')

    if 'volume' in part.columns and part['volume'].notna().any():
        volume = part['volume'].astype('float64').to_numpy()
    else:
        qv = part['quote_volume'].astype('float64').to_numpy() if 'quote_volume' in part.columns else close * 0.0
        volume = np.divide(qv, np.maximum(close, 1e-12), out=np.zeros_like(qv, dtype='float64'), where=np.maximum(close,1e-12) > 0)
    return open_, high, low, close, volume

def build_payload(df: pd.DataFrame, mode: str):
    symbols=[]; offsets=[]; pos=0
    cols = {'timestamp_s': [], 'open': [], 'high': [], 'low': [], 'close': [], 'volume': []}
    extras_acc = {}
    meta = {'ohlc_mode': mode, 'symbols': {}}
    for sym, part in df.groupby('symbol', sort=True):
        ts = pd.to_datetime(part['datetime_utc'], utc=True)
        open_, high, low, close, volume = _derive_missing_ohlcv(part, mode)
        n = len(part)
        symbols.append(sym)
        offsets.append(pos)
        pos += n
        cols['timestamp_s'].append((ts.astype('int64') // 10**9).to_numpy().astype('int64'))
        cols['open'].append(open_.astype('float64'))
        cols['high'].append(high.astype('float64'))
        cols['low'].append(low.astype('float64'))
        cols['close'].append(close.astype('float64'))
        cols['volume'].append(volume.astype('float64'))
        for col in part.columns:
            if col in BASE_COLS:
                continue
            try:
                extras_acc.setdefault(col, []).append(part[col].astype('float64').to_numpy())
            except Exception:
                pass
        meta['symbols'][sym] = {
            'rows': int(n),
            'time_from': str(part['datetime_utc'].iloc[0]),
            'time_to': str(part['datetime_utc'].iloc[-1]),
            'native_open': 'open' in part.columns,
            'native_high': 'high' in part.columns,
            'native_low': 'low' in part.columns,
            'native_volume': 'volume' in part.columns,
            'has_quote_volume': 'quote_volume' in part.columns,
        }
    payload = {'symbols': np.asarray(symbols, dtype=object), 'offsets': np.asarray(offsets, dtype=np.int64)}
    for k, parts in cols.items():
        payload[k] = np.concatenate(parts).astype(np.float64 if k != 'timestamp_s' else np.int64)
    for k, parts in extras_acc.items():
        payload[k] = np.concatenate(parts).astype(np.float64)
    return payload, meta, pos

def main():
    ap = argparse.ArgumentParser(description='Build OHLCV fast NPZ from SQLite price_indicators DB. Uses native OHLCV when present, otherwise reconstructs missing fields.')
    ap.add_argument('--db', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--symbol', default='')
    ap.add_argument('--ohlc-mode', choices=['native','prevclose','flat'], default='native')
    ap.add_argument('--meta-out', default='')
    args = ap.parse_args()
    df = _load_db(args.db, args.symbol)
    mode = 'prevclose' if args.ohlc_mode == 'native' else args.ohlc_mode
    payload, meta, rows = build_payload(df, mode)
    meta['db'] = args.db
    meta['requested_mode'] = args.ohlc_mode
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out, **payload)
    print(f'[ok] wrote {out} symbols={len(payload["symbols"])} rows={rows} mode={args.ohlc_mode}')
    meta_out = Path(args.meta_out) if args.meta_out else out.with_suffix('.meta.json')
    meta_out.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'[ok] wrote meta {meta_out}')

if __name__ == '__main__':
    main()
