#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetch all linear futures symbols from an exchange API and build compact multi-symbol fast cache.

Output NPZ format:
- symbols: array[str]
- offsets: int64, len = len(symbols)+1
- timestamp_s: concatenated per-symbol timestamps
- close: concatenated per-symbol closes

Default behavior:
- fetch enough 1m bars to build 5000 bars of 2m
- aggregate to 2m close series
- keep latest 5000 aggregated bars per symbol
"""
from __future__ import annotations
import argparse, time, os
from pathlib import Path
import numpy as np

try:
    import ccxt  # type: ignore
except Exception:
    ccxt = None


def build_exchange(name: str):
    if ccxt is None:
        raise SystemExit('ccxt is required: pip install ccxt')
    ex = getattr(ccxt, name)({'enableRateLimit': True})
    ex.load_markets()
    return ex


def list_linear_futures(ex, quote_filter=('USDT', 'USDC')):
    out = []
    for sym, m in ex.markets.items():
        if not m.get('active', True):
            continue
        if not (m.get('swap') or m.get('future')):
            continue
        if not m.get('linear', True):
            continue
        quote = str(m.get('quote') or '')
        if quote_filter and quote not in quote_filter:
            continue
        out.append(sym)
    return sorted(set(out))


def fetch_ohlcv_backfill(ex, symbol: str, tf: str, bars_needed: int, sleep_sec: float = 0.05):
    limit = 1000
    tf_ms = 60_000  # only 1m source here
    now_ms = ex.milliseconds() if hasattr(ex, 'milliseconds') else int(time.time() * 1000)
    since = now_ms - bars_needed * tf_ms - 10 * tf_ms
    rows = []
    last_ts = None
    while len(rows) < bars_needed:
        batch = ex.fetch_ohlcv(symbol, timeframe=tf, since=since, limit=limit)
        if not batch:
            break
        batch = [r for r in batch if last_ts is None or r[0] > last_ts]
        if not batch:
            break
        rows.extend(batch)
        last_ts = batch[-1][0]
        since = last_ts + tf_ms
        if len(batch) < limit:
            break
        time.sleep(sleep_sec)
    if not rows:
        return []
    rows = sorted({r[0]: r for r in rows}.values(), key=lambda x: x[0])
    return rows[-bars_needed:]


def aggregate_to_2m(ohlcv_1m, target_bars=5000):
    buckets = {}
    for ts, o, h, l, c, v in ohlcv_1m:
        b = (int(ts) // 120000) * 120000
        buckets[b] = float(c)  # close-only cache
    items = sorted(buckets.items())[-target_bars:]
    ts = np.array([t // 1000 for t, _ in items], dtype=np.int64)
    close = np.array([c for _, c in items], dtype=np.float64)
    return ts, close


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--exchange', default='bingx')
    ap.add_argument('--out', required=True)
    ap.add_argument('--bars-2m', type=int, default=5000)
    ap.add_argument('--source-tf', default='1m')
    ap.add_argument('--quotes', default='USDT,USDC')
    ap.add_argument('--sleep-sec', type=float, default=0.05)
    args = ap.parse_args()

    ex = build_exchange(args.exchange)
    quotes = tuple(q.strip().upper() for q in args.quotes.split(',') if q.strip())
    symbols = list_linear_futures(ex, quote_filter=quotes)
    print(f'[symbols] futures={len(symbols)} quotes={quotes}')

    need_1m = args.bars_2m * 2 + 20
    all_symbols = []
    offsets = [0]
    all_ts = []
    all_close = []
    for i, sym in enumerate(symbols, 1):
        try:
            raw = fetch_ohlcv_backfill(ex, sym, args.source_tf, need_1m, sleep_sec=args.sleep_sec)
            if len(raw) < 50:
                print(f'[skip] {sym} too_few_rows={len(raw)}')
                continue
            ts, close = aggregate_to_2m(raw, target_bars=args.bars_2m)
            if len(ts) == 0:
                print(f'[skip] {sym} empty_after_agg')
                continue
            all_symbols.append(sym)
            all_ts.append(ts)
            all_close.append(close)
            offsets.append(offsets[-1] + len(ts))
            print(f'[ok] {i}/{len(symbols)} {sym} bars={len(ts)} range={ts[0]}..{ts[-1]}')
        except Exception as e:
            print(f'[err] {i}/{len(symbols)} {sym}: {e}')

    if not all_symbols:
        raise SystemExit('No symbols fetched')

    np.savez_compressed(
        args.out,
        symbols=np.array(all_symbols, dtype=object),
        offsets=np.array(offsets, dtype=np.int64),
        timestamp_s=np.concatenate(all_ts).astype(np.int64),
        close=np.concatenate(all_close).astype(np.float64),
    )
    print(f'[done] wrote {args.out} symbols={len(all_symbols)} total_rows={offsets[-1]}')

if __name__ == '__main__':
    main()
