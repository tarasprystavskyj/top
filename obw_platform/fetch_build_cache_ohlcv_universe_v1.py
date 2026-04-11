#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import argparse, os, sys, sqlite3, time
from typing import Any, Dict, List
import numpy as np
import pandas as pd
try:
    import ccxt  # type: ignore
except Exception:
    ccxt = None

# lightweight copies from existing helpers

def normalize_token(s: str) -> str:
    return str(s).strip().upper()

def _clean_symbol_entry(raw: str):
    s = str(raw).strip()
    if not s or s.lower() in {'symbol','symbols'} or s.startswith('#'):
        return None
    return s

def load_universe_symbols(path: str) -> List[str]:
    syms=[]
    with open(path,'r',encoding='utf-8') as f:
        for line in f:
            c=_clean_symbol_entry(line)
            if c: syms.append(c)
    out=[]; seen=set()
    for s in syms:
        k=s.upper()
        if k in seen: continue
        seen.add(k); out.append(s)
    return out

def parse_base_quote(raw: str):
    s = normalize_token(raw)
    if '/' in s:
        base, rest = s.split('/', 1)
        return base, rest.split(':')[0]
    if s.endswith('USDT') and len(s) > 4:
        return s[:-4], 'USDT'
    if s.endswith('USDC') and len(s) > 4:
        return s[:-4], 'USDC'
    return s, None

def resolve_market(ex, raw: str, fmt_bias: str = 'auto'):
    s = normalize_token(raw)
    markets = ex.markets if getattr(ex,'markets',None) else ex.load_markets()
    if s in markets: return s
    base, guess = parse_base_quote(s)
    ladders = {
        'auto':[f'{base}/USDT:USDT', f'{base}/USDT', f'{base}/USDC:USDC', f'{base}/USDC'],
        'usdtm':[f'{base}/USDT:USDT', f'{base}/USDC:USDC', f'{base}/USDT', f'{base}/USDC'],
        'usdt':[f'{base}/USDT', f'{base}/USDT:USDT', f'{base}/USDC', f'{base}/USDC:USDC'],
    }
    cand=ladders.get(fmt_bias,ladders['auto'])
    if guess in {'USDT','USDC'}:
        cand = [c for c in cand if guess in c] + [c for c in cand if guess not in c]
    for c in cand:
        if c in markets: return c
    return None

def ensure_schema(db_path: str) -> None:
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute(
        '''CREATE TABLE IF NOT EXISTS price_indicators(
            symbol TEXT,
            datetime_utc TEXT,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            rsi REAL, stochastic REAL, mfi REAL, overbought_index REAL,
            atr_ratio REAL,
            gain_24h_before REAL,
            dp6h REAL, dp12h REAL,
            quote_volume REAL, qv_24h REAL, vol_surge_mult REAL,
            PRIMARY KEY (symbol, datetime_utc)
        )'''
    )
    cur.execute('PRAGMA journal_mode=WAL;')
    con.commit(); con.close()

def insert_rows(db_path: str, rows: List[dict]) -> None:
    if not rows: return
    cols=["symbol", "datetime_utc", "open", "high", "low", "close", "volume","rsi", "stochastic", "mfi", "overbought_index", "atr_ratio", "gain_24h_before","dp6h", "dp12h", "quote_volume", "qv_24h", "vol_surge_mult"]
    con=sqlite3.connect(db_path); cur=con.cursor()
    cur.executemany(f"INSERT OR REPLACE INTO price_indicators ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})", [tuple(r[c] for c in cols) for r in rows])
    con.commit(); con.close()

def compute_features(df: pd.DataFrame, tf_seconds: int = 60) -> pd.DataFrame:
    out = df.copy()
    bars_24h = max(1, int(round(24 * 3600 / max(1, tf_seconds))))
    bars_12h = max(1, int(round(12 * 3600 / max(1, tf_seconds))))
    bars_6h = max(1, int(round(6 * 3600 / max(1, tf_seconds))))
    out['gain_24h_before'] = (out['close'] / out['close'].shift(bars_24h) - 1.0).replace([np.inf,-np.inf],np.nan).fillna(0.0)
    out['dp6h'] = (out['close'] / out['close'].shift(bars_6h) - 1.0).replace([np.inf,-np.inf],np.nan).fillna(0.0)
    out['dp12h'] = (out['close'] / out['close'].shift(bars_12h) - 1.0).replace([np.inf,-np.inf],np.nan).fillna(0.0)
    prev_close = out['close'].shift(1)
    tr = pd.concat([(out['high']-out['low']).abs(), (out['high']-prev_close).abs(), (out['low']-prev_close).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/14, adjust=False).mean()
    out['atr_ratio'] = (atr / out['close']).replace([np.inf,-np.inf],np.nan).fillna(0.0)
    out['quote_volume'] = (out['volume'] * out['close']).replace([np.inf,-np.inf],np.nan).fillna(0.0)
    out['qv_24h'] = out['quote_volume'].rolling(bars_24h, min_periods=1).sum()
    avg_per_bar = out['qv_24h'] / float(bars_24h)
    out['vol_surge_mult'] = np.where(avg_per_bar > 0, out['quote_volume'] / avg_per_bar, 0.0)
    out['rsi']=0.0; out['stochastic']=0.0; out['mfi']=0.0; out['overbought_index']=0.0
    return out

def fetch_symbol_ohlcv(ex, market: str, timeframe: str, bars: int, sleep_sec: float) -> pd.DataFrame:
    all_rows=[]; limit=min(1000,bars)
    since=None
    while len(all_rows) < bars:
        batch = ex.fetch_ohlcv(market, timeframe=timeframe, since=since, limit=limit)
        if not batch: break
        if since is not None and batch[0][0] <= since and len(batch)==1: break
        if all_rows and batch[0][0] <= all_rows[-1][0]:
            batch=[r for r in batch if r[0] > all_rows[-1][0]]
        if not batch: break
        all_rows.extend(batch)
        since = batch[-1][0] + 60_000  # for 1m
        time.sleep(sleep_sec)
        if len(batch) < limit: break
    if len(all_rows) > bars:
        all_rows = all_rows[-bars:]
    df = pd.DataFrame(all_rows, columns=['timestamp_ms','open','high','low','close','volume'])
    return df

def main():
    ap=argparse.ArgumentParser(description='Fetch 1m OHLCV for a universe and build standard SQLite DB')
    ap.add_argument('--universe-file', required=True)
    ap.add_argument('--output', required=True)
    ap.add_argument('--exchange', default='bingx')
    ap.add_argument('--symbol-format', choices=['auto','usdtm','usdt'], default='usdtm')
    ap.add_argument('--timeframe', default='1m')
    ap.add_argument('--bars', type=int, default=5000)
    ap.add_argument('--sleep-sec', type=float, default=0.15)
    args=ap.parse_args()
    if ccxt is None:
        raise SystemExit('ccxt not installed')
    ex_cls = getattr(ccxt, args.exchange)
    ex = ex_cls({'enableRateLimit': True})
    ex.load_markets()
    ensure_schema(args.output)
    syms=load_universe_symbols(args.universe_file)
    total=0
    for i, raw in enumerate(syms, start=1):
        mkt = resolve_market(ex, raw, fmt_bias=args.symbol_format)
        if not mkt:
            print(f'[skip] {i}/{len(syms)} {raw} unresolved', file=sys.stderr)
            continue
        try:
            df = fetch_symbol_ohlcv(ex, mkt, args.timeframe, args.bars, args.sleep_sec)
            if df.empty:
                print(f'[skip] {i}/{len(syms)} {mkt} empty', file=sys.stderr)
                continue
            df['datetime_utc'] = pd.to_datetime(df['timestamp_ms'], unit='ms', utc=True).dt.strftime('%Y-%m-%dT%H:%M:%S+00:00')
            feats = compute_features(df.set_index('datetime_utc')[['open','high','low','close','volume']], tf_seconds=60)
            rows=[]
            for idx, r in feats.iterrows():
                rows.append({
                    'symbol': mkt, 'datetime_utc': idx,
                    'open': float(r['open']), 'high': float(r['high']), 'low': float(r['low']), 'close': float(r['close']), 'volume': float(r['volume']),
                    'rsi': float(r['rsi']), 'stochastic': float(r['stochastic']), 'mfi': float(r['mfi']), 'overbought_index': float(r['overbought_index']),
                    'atr_ratio': float(r['atr_ratio']), 'gain_24h_before': float(r['gain_24h_before']),
                    'dp6h': float(r['dp6h']), 'dp12h': float(r['dp12h']), 'quote_volume': float(r['quote_volume']), 'qv_24h': float(r['qv_24h']), 'vol_surge_mult': float(r['vol_surge_mult']),
                })
            insert_rows(args.output, rows)
            total += len(rows)
            print(f'[ok] {i}/{len(syms)} {mkt} bars={len(rows)} range={rows[0]["datetime_utc"]}..{rows[-1]["datetime_utc"]}')
        except Exception as e:
            print(f'[err] {i}/{len(syms)} {mkt}: {e}', file=sys.stderr)
    print(f'[done] total_rows={total} db={args.output}')

if __name__=='__main__':
    main()
