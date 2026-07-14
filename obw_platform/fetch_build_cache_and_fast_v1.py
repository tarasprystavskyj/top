#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_build_cache_and_fast_v1.py

Unified cache builder for the OBW platform.

What this script can do
=======================
1) Fetch OHLCV from exchange API and build:
   - standard SQLite DB,
   - fast NPZ,
   - or both in one run.

2) Fetch raw trades/ticks from exchange API and aggregate them into arbitrary bars,
   including sub-minute bars such as 1s, 5s, 10s, 30s.

3) Read local tick JSONL files (for example produced by ticks_to_1s_json_v3.py),
   aggregate them into arbitrary bars, and build DB / NPZ without re-downloading data.

Typical source modes
====================
--source auto
    Default. Chooses source automatically:
      - if --ticks-dir is provided -> local_ticks
      - else if timeframe < 1m      -> trades_api
      - else                        -> ohlcv_api

--source ohlcv_api
    Use exchange fetch_ohlcv(). Best for 1m and higher bars.

--source trades_api
    Use exchange fetch_trades() and build bars from ticks. Required for sub-minute bars
    when downloading directly from exchange API.

--source local_ticks
    Read local monthly tick JSONL files and build bars locally.

Outputs
=======
--db-out PATH
    Write the standard SQLite DB with price_indicators schema.

--npz-out PATH
    Write fast compressed NPZ used by the fast backtester/tuner.

--npz-only
    Skip DB writing entirely and generate only NPZ.

Feature controls
================
--feature-set full
    Compute the standard cached columns:
      atr_ratio, gain_24h_before, dp6h, dp12h, quote_volume, qv_24h, vol_surge_mult
    and keep rsi/stochastic/mfi/overbought_index as zero placeholders.

--feature-set none
    Do not spend time computing optional indicators. Keep only OHLCV meaningful,
    and write zeros into schema compatibility fields.

--cache-pack-trend
    Precompute trend-pack fields used by strategies/cryptomine_pack_dual_full.py:
      trend_ma, trend_ma_prev, trend_slope_pct,
      trend_target_pct_long, trend_target_pct_short

Current recommended usage
=========================
A) Build 1 year of 1m COMP from exchange OHLCV into DB + NPZ

  python3 obw_platform/fetch_build_cache_and_fast_v1.py \
    -i obw_platform/universe/universe_COMP_1m.txt \
    -t 1m \
    --back-bars 525600 \
    --exchange bingx \
    --ccxt-symbol-format usdtm \
    --db-out DB/combined_cache_1m_COMP_1y.db \
    --npz-out DB/fast_cache_1m_COMP_1y.npz \
    --feature-set none

B) Build only NPZ, skip SQLite DB completely

  python3 obw_platform/fetch_build_cache_and_fast_v1.py \
    -i obw_platform/universe/universe_COMP_1m.txt \
    -t 1m \
    --back-bars 525600 \
    --exchange bingx \
    --ccxt-symbol-format usdtm \
    --npz-out DB/fast_cache_1m_COMP_1y.npz \
    --npz-only \
    --feature-set none

C) Build 30s bars directly from exchange trade API

  python3 obw_platform/fetch_build_cache_and_fast_v1.py \
    -i obw_platform/universe/universe_COMP_1m.txt \
    -t 30s \
    --start "2026-03-07 00:00" \
    --end   "2026-03-15 00:00" \
    --exchange bybit \
    --ccxt-symbol-format usdtm \
    --source trades_api \
    --db-out DB/combined_cache_30s_COMP.db \
    --npz-out DB/fast_cache_30s_COMP.npz \
    --feature-set none

D) Build 30s bars from local monthly tick JSONL files

  python3 obw_platform/fetch_build_cache_and_fast_v1.py \
    --source local_ticks \
    --ticks-dir DB/ENAUSDT-bybit-2025-03-01-2026-03-01-20260316_190351 \
    --timeframe 30s \
    --db-out DB/combined_cache_30s_ENA_6m.db \
    --npz-out DB/fast_cache_30s_ENA_6m.npz \
    --market-symbol ENA/USDT:USDT \
    --month-from 2025-09 \
    --month-to 2026-02 \
    --feature-set none

E) Build 1s bars from local ticks for an exact UTC interval

  python3 obw_platform/fetch_build_cache_and_fast_v1.py \
    --source local_ticks \
    --ticks-dir DB/ENAUSDT-okx-2026-03-07-2026-03-15-20260316_190351 \
    --timeframe 1s \
    --start "2026-03-07 00:00:00" \
    --end   "2026-03-15 00:00:00" \
    --db-out DB/combined_cache_1s_ENA.db \
    --npz-out DB/fast_cache_1s_ENA.npz \
    --feature-set none

F) Same as above, but also cache trend pack fields for pack dual strategy

  python3 obw_platform/fetch_build_cache_and_fast_v1.py \
    -i obw_platform/universe/universe_ENA_1m.txt \
    -t 1m \
    --back-bars 525600 \
    --exchange bingx \
    --ccxt-symbol-format usdtm \
    --db-out DB/combined_cache_1m_ENA_1y.db \
    --npz-out DB/fast_cache_1m_ENA_1y.npz \
    --feature-set none \
    --cache-pack-trend

Notes
=====
- Output is quiet by default. Use --debug to enable incremental progress with flush=True and
  line-buffering enabled, so long runs show activity immediately instead of waiting for a full buffer.
- Sub-minute bars require either:
    * --source trades_api, or
    * --source local_ticks / --ticks-dir ...
- For exchange OHLCV mode, 1m+ is recommended.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

try:
    import ccxt  # type: ignore
except Exception:
    ccxt = None


DEBUG = False
ARGS = argparse.Namespace(max_retries=8, sleep_sec=0.15)


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except Exception:
            pass


def log(*parts, file=None, force: bool = False) -> None:
    if not (DEBUG or force):
        return
    print(*parts, file=file or sys.stdout, flush=True)


def info(*parts, file=None) -> None:
    print(*parts, file=file or sys.stdout, flush=True)


_configure_stdio()


# -------- symbol helpers --------

def normalize_token(s: str) -> str:
    return str(s).strip().upper()


def _clean_symbol_entry(raw: str) -> Optional[str]:
    s = str(raw).strip()
    if not s or s.lower() in {"symbol", "symbols"} or s.startswith("#"):
        return None
    return s


def load_universe_symbols(path: str) -> List[str]:
    if not os.path.exists(path):
        raise SystemExit(f"Universe file not found: {path}")
    symbols: List[str] = []
    try:
        df = pd.read_csv(path)
    except Exception:
        df = None
    if df is not None and not df.empty:
        cols = {str(c).strip().lower(): str(c) for c in df.columns}
        if "symbol" in cols:
            col = cols["symbol"]
            for val in df[col].tolist():
                s = _clean_symbol_entry(val)
                if s:
                    symbols.append(s)
        elif df.shape[1] == 1:
            header = _clean_symbol_entry(df.columns[0])
            if header:
                symbols.append(header)
            for val in df.iloc[:, 0].tolist():
                s = _clean_symbol_entry(val)
                if s:
                    symbols.append(s)
    if not symbols:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                s = _clean_symbol_entry(line)
                if s:
                    symbols.append(s)
    out: List[str] = []
    seen = set()
    for s in symbols:
        k = s.upper()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    if not out:
        raise SystemExit(f"No symbols found in universe file: {path}")
    return out


def parse_base_quote(raw: str) -> Tuple[str, Optional[str]]:
    s = normalize_token(raw)
    if '/' in s:
        base, rest = s.split('/', 1)
        return base, rest.split(':')[0]
    if '-' in s:
        parts = s.split('-')
        if len(parts) >= 2:
            return parts[0], parts[1]
    if s.endswith('USDT') and len(s) > 4:
        return s[:-4], 'USDT'
    if s.endswith('USDC') and len(s) > 4:
        return s[:-4], 'USDC'
    return s, None


def resolve_market(ex, raw: str, fmt_bias: str = 'auto') -> Optional[str]:
    s = normalize_token(raw)
    markets = ex.markets if getattr(ex, 'markets', None) else ex.load_markets()
    if s in markets:
        return s
    base, guess = parse_base_quote(s)
    ladders = {
        'auto':      [f'{base}/USDT:USDT', f'{base}/USDT', f'{base}/USDC:USDC', f'{base}/USDC'],
        'usdtm':     [f'{base}/USDT:USDT', f'{base}/USDC:USDC', f'{base}/USDT', f'{base}/USDC'],
        'usdt':      [f'{base}/USDT', f'{base}/USDT:USDT', f'{base}/USDC', f'{base}/USDC:USDC'],
        'spot_only': [f'{base}/USDT', f'{base}/USDC'],
        'perp_only': [f'{base}/USDT:USDT', f'{base}/USDC:USDC'],
    }
    cand = ladders.get(fmt_bias, ladders['auto'])
    if guess in {'USDT', 'USDC'}:
        cand = [c for c in cand if guess in c] + [c for c in cand if guess not in c]
    for c in cand:
        if c in markets:
            return c
    return None


# -------- timeframe helpers --------

TF_ALIASES = {
    '1sec': '1s', '1 second': '1s', '1 seconds': '1s',
    '5sec': '5s', '5 seconds': '5s',
    '10sec': '10s', '10 seconds': '10s',
    '15sec': '15s', '15 seconds': '15s',
    '30sec': '30s', '30 seconds': '30s',
    '1min': '1m', '1 minute': '1m', '1 minutes': '1m',
    '3min': '3m', '3 minutes': '3m',
    '5min': '5m', '5 minutes': '5m', '5 mins': '5m',
    '15min': '15m', '15 minutes': '15m',
    '30min': '30m', '30 minutes': '30m',
    '45min': '45m', '45 minutes': '45m',
    '60min': '1h', '60 minutes': '1h',
    '1hour': '1h', '1 hr': '1h',
    '2hour': '2h', '2 hr': '2h',
    '4hour': '4h', '4 hr': '4h',
    '6hour': '6h', '6 hr': '6h',
    '12hour': '12h', '12 hr': '12h',
    '24hour': '1d', '24 hr': '1d',
}


def normalize_timeframe(tf: str) -> str:
    s = tf.strip().lower().replace('_', '').replace('-', ' ').replace('/', ' ').replace('.', '')
    return TF_ALIASES.get(s, tf.strip().lower())


def timeframe_to_seconds(tf: str) -> int:
    tf = normalize_timeframe(tf).strip()
    if len(tf) < 2:
        raise ValueError(f'Bad timeframe: {tf!r}')
    unit = tf[-1]
    try:
        n = int(tf[:-1])
    except Exception as exc:
        raise ValueError(f'Bad timeframe: {tf!r}') from exc
    mult = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}
    if unit not in mult:
        raise ValueError(f'Unsupported timeframe: {tf}')
    return n * mult[unit]


def timeframe_to_milliseconds(tf: str) -> int:
    return timeframe_to_seconds(tf) * 1000


# -------- schema / feature helpers --------

BASE_EXTRA_COLUMNS = [
    'rsi', 'stochastic', 'mfi', 'overbought_index',
    'atr_ratio', 'gain_24h_before', 'dp6h', 'dp12h',
    'quote_volume', 'qv_24h', 'vol_surge_mult',
]

TREND_EXTRA_COLUMNS = [
    'trend_ma', 'trend_ma_prev', 'trend_slope_pct',
    'trend_target_pct_long', 'trend_target_pct_short',
]


def ensure_schema(db_path: str, include_trend_columns: bool = False) -> None:
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute(
        """CREATE TABLE IF NOT EXISTS price_indicators(
            symbol TEXT,
            datetime_utc TEXT,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            rsi REAL, stochastic REAL, mfi REAL, overbought_index REAL,
            atr_ratio REAL,
            gain_24h_before REAL,
            dp6h REAL, dp12h REAL,
            quote_volume REAL, qv_24h REAL, vol_surge_mult REAL,
            PRIMARY KEY (symbol, datetime_utc)
        )"""
    )
    if include_trend_columns:
        existing = {row[1] for row in cur.execute('PRAGMA table_info(price_indicators)')}
        for col in TREND_EXTRA_COLUMNS:
            if col not in existing:
                cur.execute(f'ALTER TABLE price_indicators ADD COLUMN {col} REAL')
    cur.execute('PRAGMA journal_mode=WAL;')
    con.commit()
    con.close()


def insert_rows(db_path: str, rows: List[dict], include_trend_columns: bool = False) -> None:
    if not rows:
        return
    cols = [
        'symbol', 'datetime_utc', 'open', 'high', 'low', 'close', 'volume',
        'rsi', 'stochastic', 'mfi', 'overbought_index', 'atr_ratio',
        'gain_24h_before', 'dp6h', 'dp12h', 'quote_volume', 'qv_24h', 'vol_surge_mult',
    ]
    if include_trend_columns:
        cols += TREND_EXTRA_COLUMNS
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    ph = ','.join(['?'] * len(cols))
    cur.executemany(
        f"INSERT OR REPLACE INTO price_indicators ({','.join(cols)}) VALUES ({ph})",
        [tuple(r.get(c) for c in cols) for r in rows],
    )
    con.commit()
    con.close()


def calc_atr_ratio(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df['close'].shift(1)
    tr = pd.concat([
        (df['high'] - df['low']).abs(),
        (df['high'] - prev_close).abs(),
        (df['low'] - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    return (atr / df['close']).replace([np.inf, -np.inf], np.nan).fillna(0.0)


def compute_base_features(df: pd.DataFrame, tf_seconds: int, feature_set: str = 'full') -> pd.DataFrame:
    out = df.copy()
    if feature_set == 'none':
        for col in BASE_EXTRA_COLUMNS:
            out[col] = 0.0
        return out
    bars_24h = max(1, int(round(24 * 3600 / max(1, tf_seconds))))
    bars_12h = max(1, int(round(12 * 3600 / max(1, tf_seconds))))
    bars_6h = max(1, int(round(6 * 3600 / max(1, tf_seconds))))
    out['gain_24h_before'] = (out['close'] / out['close'].shift(bars_24h) - 1.0).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    out['dp6h'] = (out['close'] / out['close'].shift(bars_6h) - 1.0).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    out['dp12h'] = (out['close'] / out['close'].shift(bars_12h) - 1.0).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    out['atr_ratio'] = calc_atr_ratio(out, 14)
    out['quote_volume'] = (out['volume'] * out['close']).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    out['qv_24h'] = out['quote_volume'].rolling(bars_24h, min_periods=1).sum()
    avg_per_bar = out['qv_24h'] / float(bars_24h)
    with np.errstate(divide='ignore', invalid='ignore'):
        out['vol_surge_mult'] = np.where(avg_per_bar > 0, out['quote_volume'] / avg_per_bar, 0.0)
    out['rsi'] = 0.0
    out['stochastic'] = 0.0
    out['mfi'] = 0.0
    out['overbought_index'] = 0.0
    return out


def _trend_bucket_id(ts: pd.Timestamp, trend_tf: str) -> str:
    ts = pd.Timestamp(ts)
    ts = ts.tz_convert('UTC') if ts.tzinfo else ts.tz_localize('UTC')
    tf = str(trend_tf).upper()
    if tf == 'W':
        iso = ts.isocalendar()
        try:
            year = int(getattr(iso, 'year'))
            week = int(getattr(iso, 'week'))
        except Exception:
            year = int(iso[0])
            week = int(iso[1])
        return f'{year}-W{week:02d}'
    if tf == 'D':
        return ts.strftime('%Y-%m-%d')
    secs = timeframe_to_seconds(tf.lower())
    return str(int(ts.timestamp()) // max(secs, 1))


def compute_pack_trend_features(
    close_indexed: pd.DataFrame,
    *,
    trend_tf: str,
    trend_ma_len: int,
    trend_slope_bars: int,
    trend_slope_long_bound_pct: float,
    trend_slope_short_bound_pct: float,
    trend_score_min_pct: float,
    trend_score_max_pct: float,
    min_long_invest_pct: float,
    max_long_invest_pct: float,
    min_short_invest_pct: float,
    max_short_invest_pct: float,
) -> pd.DataFrame:
    closes = close_indexed['close'].astype(float).to_numpy()
    idx = pd.to_datetime(close_indexed.index, utc=True)
    trend_ma = np.full(len(close_indexed), np.nan, dtype=np.float64)
    trend_ma_prev = np.full(len(close_indexed), np.nan, dtype=np.float64)
    trend_slope = np.zeros(len(close_indexed), dtype=np.float64)
    tgt_long = np.full(len(close_indexed), float(min_long_invest_pct), dtype=np.float64)
    tgt_short = np.full(len(close_indexed), float(min_short_invest_pct), dtype=np.float64)

    bucket = None
    htf_closes: List[float] = []
    ma_series: List[float] = []
    cur_close = None
    rng = max(abs(trend_slope_long_bound_pct - trend_slope_short_bound_pct), 1e-9)
    score_rng = max(abs(trend_score_max_pct - trend_score_min_pct), 1e-9)

    for i, (ts, close) in enumerate(zip(idx, closes)):
        b = _trend_bucket_id(ts, trend_tf)
        if bucket is None:
            bucket = b
            cur_close = float(close)
        elif b != bucket:
            if cur_close is not None:
                htf_closes.append(float(cur_close))
            bucket = b
            cur_close = float(close)
        else:
            cur_close = float(close)

        vals = htf_closes + ([float(cur_close)] if cur_close is not None else [])
        ma = float(np.mean(vals[-trend_ma_len:])) if len(vals) >= trend_ma_len else np.nan
        trend_ma[i] = ma
        ma_series.append(ma)
        prev = ma_series[-1 - trend_slope_bars] if len(ma_series) > trend_slope_bars else np.nan
        trend_ma_prev[i] = prev
        slope = ((ma - prev) / prev) * 100.0 if np.isfinite(ma) and np.isfinite(prev) and abs(prev) > 1e-12 else 0.0
        trend_slope[i] = float(slope)

        strength_long = max(0.0, min(100.0, 100.0 * (slope - trend_slope_short_bound_pct) / rng))
        strength_short = max(0.0, min(100.0, 100.0 * (trend_slope_long_bound_pct - slope) / rng))
        factor_long = max(0.0, min(1.0, (strength_long - trend_score_min_pct) / score_rng))
        factor_short = max(0.0, min(1.0, (strength_short - trend_score_min_pct) / score_rng))
        tgt_long[i] = float(min_long_invest_pct + (max_long_invest_pct - min_long_invest_pct) * factor_long)
        tgt_short[i] = float(min_short_invest_pct + (max_short_invest_pct - min_short_invest_pct) * factor_short)

    out = close_indexed.copy()
    out['trend_ma'] = trend_ma
    out['trend_ma_prev'] = trend_ma_prev
    out['trend_slope_pct'] = trend_slope
    out['trend_target_pct_long'] = tgt_long
    out['trend_target_pct_short'] = tgt_short
    return out


# -------- fetch helpers --------

MAX_PER_REQUEST = 1000
FILE_RE = re.compile(
    r"^(?P<pair>[A-Z0-9]+)-(?P<exchange>[a-z0-9_]+)-(?P<start>\d{4}-\d{2}-\d{2})-(?P<end>\d{4}-\d{2}-\d{2})-(?P<month>\d{4}-\d{2})\.jsonl(?:\.txt)?$"
)


def parse_dt_to_ms_utc(s: str) -> int:
    ts = pd.to_datetime(s, utc=True)
    return int(ts.value // 10**6)


def df_from_ohlcv(ohlcv: Sequence[Sequence[float]]) -> pd.DataFrame:
    if not ohlcv:
        return pd.DataFrame()
    df = pd.DataFrame(ohlcv, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
    df['datetime_utc'] = pd.to_datetime(df['ts'], unit='ms', utc=True).dt.strftime('%Y-%m-%dT%H:%M:%S+00:00')
    return df.set_index('datetime_utc')[['open', 'high', 'low', 'close', 'volume']].astype(float)


def _datetime_index_epoch_s(index) -> np.ndarray:
    raw = pd.to_datetime(index, utc=True).astype('int64').to_numpy()
    if raw.size == 0:
        return raw.astype(np.int64)
    max_abs = int(np.nanmax(np.abs(raw)))
    if max_abs > 10**17:   # nanoseconds
        return (raw // 1_000_000_000).astype(np.int64)
    if max_abs > 10**14:   # microseconds
        return (raw // 1_000_000).astype(np.int64)
    if max_abs > 10**11:   # milliseconds
        return (raw // 1_000).astype(np.int64)
    return raw.astype(np.int64)


def _datetime_index_epoch_ms(index) -> np.ndarray:
    return (_datetime_index_epoch_s(index) * 1000).astype(np.int64)


def fetch_ohlcv_range(ex, market: str, timeframe: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    tf_ms = timeframe_to_milliseconds(timeframe)
    if end_ms <= start_ms:
        return pd.DataFrame()
    cursor = start_ms
    frames = []
    req_no = 0
    while cursor < end_ms:
        req_no += 1
        remaining_ms = max(0, end_ms - cursor)
        est_rem_bars = int(np.ceil(remaining_ms / max(tf_ms, 1)))
        limit = min(MAX_PER_REQUEST, max(1, est_rem_bars))
        log(f'[fetch ohlcv] {market} req={req_no} since={pd.to_datetime(cursor, unit="ms", utc=True)} limit={limit}')
        ohlcv = None
        for attempt in range(max(1, int(getattr(ARGS, 'max_retries', 1)))):
            try:
                ohlcv = ex.fetch_ohlcv(market, timeframe=timeframe, since=cursor, limit=limit)
                break
            except Exception as exc:
                if ccxt is not None and not isinstance(exc, ccxt.BaseError):
                    raise
                wait_s = max(float(getattr(ARGS, 'sleep_sec', 0.15)), 0.1) * (2 ** min(attempt, 5))
                info(
                    f'[retry ohlcv] {market} req={req_no} attempt={attempt + 1}/{getattr(ARGS, "max_retries", 1)} '
                    f'wait={wait_s:.2f}s error={type(exc).__name__}: {exc}',
                    file=sys.stderr,
                )
                time.sleep(wait_s)
        if ohlcv is None:
            info(f'[fetch ohlcv] {market} failed after {getattr(ARGS, "max_retries", 1)} retries -> stop', file=sys.stderr)
            break
        if not ohlcv:
            log(f'[fetch ohlcv] {market} empty_batch -> stop')
            break
        df = df_from_ohlcv(ohlcv)
        if df.empty:
            log(f'[fetch ohlcv] {market} empty_df -> stop')
            break
        frames.append(df)
        last_ts = int(pd.to_datetime(df.index[-1], utc=True).value // 10**6)
        next_cursor = last_ts + tf_ms
        log(f'[fetch ohlcv] {market} batch_rows={len(df)} last={df.index[-1]} next={pd.to_datetime(next_cursor, unit="ms", utc=True)}')
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if next_cursor >= end_ms:
            break
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames).sort_index()
    out = out[~out.index.duplicated(keep='last')]
    idx_ms = _datetime_index_epoch_ms(out.index)
    mask = (idx_ms >= start_ms) & (idx_ms < end_ms)
    return out.loc[mask]


def _trade_price(t: Dict[str, Any]) -> Optional[float]:
    if t.get('price') is not None:
        return float(t['price'])
    info = t.get('info', {})
    if isinstance(info, dict):
        for k in ('price', 'px', 'p'):
            if info.get(k) is not None:
                return float(info[k])
    return None


def _trade_amount(t: Dict[str, Any]) -> float:
    if t.get('amount') is not None:
        return float(t['amount'])
    info = t.get('info', {})
    if isinstance(info, dict):
        for k in ('size', 'qty', 'sz', 'q', 'amount', 'v'):
            if info.get(k) is not None:
                try:
                    return float(info[k])
                except Exception:
                    pass
    return 0.0


def fetch_trades_range(
    ex,
    market: str,
    start_ms: int,
    end_ms: int,
    *,
    limit_per_call: int = 1000,
    max_empty_tries: int = 3,
    sleep_sec: float = 0.15,
) -> pd.DataFrame:
    all_rows: List[Tuple[int, float, float, str]] = []
    cursor = start_ms
    empty_tries = 0
    last_seen_key = None
    req_no = 0

    while cursor < end_ms:
        req_no += 1
        log(f'[fetch trades] {market} req={req_no} since={pd.to_datetime(cursor, unit="ms", utc=True)} limit={limit_per_call}')
        trades = ex.fetch_trades(market, since=cursor, limit=limit_per_call)
        if not trades:
            empty_tries += 1
            log(f'[fetch trades] {market} empty_batch tries={empty_tries}/{max_empty_tries}')
            if empty_tries >= max_empty_tries:
                break
            time.sleep(sleep_sec)
            cursor += 1000
            continue

        empty_tries = 0
        batch_rows: List[Tuple[int, float, float, str]] = []
        for t in trades:
            ts = t.get('timestamp')
            px = _trade_price(t)
            amt = _trade_amount(t)
            if ts is None or px is None:
                continue
            if ts < start_ms or ts >= end_ms:
                continue
            trade_id = str(t.get('id') or f'{ts}-{px}-{amt}')
            batch_rows.append((int(ts), float(px), float(amt), trade_id))

        if not batch_rows:
            cursor += 1000
            time.sleep(sleep_sec)
            continue

        batch_rows = sorted(set(batch_rows), key=lambda x: (x[0], x[3]))
        all_rows.extend(batch_rows)
        last_ts = batch_rows[-1][0]
        last_key = (batch_rows[-1][0], batch_rows[-1][3])
        log(f'[fetch trades] {market} batch_trades={len(batch_rows)} last_ts={pd.to_datetime(last_ts, unit="ms", utc=True)}')

        if last_seen_key == last_key:
            cursor = last_ts + 1
        else:
            cursor = last_ts + 1
            last_seen_key = last_key
        time.sleep(sleep_sec)

    if not all_rows:
        return pd.DataFrame(columns=['timestamp', 'price', 'amount'])

    df = pd.DataFrame(all_rows, columns=['timestamp', 'price', 'amount', 'trade_id'])
    df = df.sort_values(['timestamp', 'trade_id']).drop_duplicates(subset=['timestamp', 'trade_id'], keep='last')
    return df[['timestamp', 'price', 'amount']].reset_index(drop=True)


def aggregate_trades_to_bars(trades_df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    if trades_df.empty:
        return pd.DataFrame(columns=['open', 'high', 'low', 'close', 'volume'])
    tf_ms = timeframe_to_milliseconds(timeframe)
    work = trades_df.copy()
    work['bucket'] = (work['timestamp'] // tf_ms) * tf_ms
    grouped = work.groupby('bucket', sort=True)
    bars = pd.DataFrame({
        'open': grouped['price'].first(),
        'high': grouped['price'].max(),
        'low': grouped['price'].min(),
        'close': grouped['price'].last(),
        'volume': grouped['amount'].sum(),
    })
    bars.index = pd.to_datetime(bars.index, unit='ms', utc=True).strftime('%Y-%m-%dT%H:%M:%S+00:00')
    return bars.astype(float)


# -------- local tick helpers --------

def discover_files(ticks_dir: Path) -> List[Path]:
    files: List[Path] = []
    for p in ticks_dir.iterdir():
        if p.is_file() and (p.name.endswith('.jsonl') or p.name.endswith('.jsonl.txt')):
            files.append(p)
    if not files:
        raise SystemExit(f'No .jsonl files found in {ticks_dir}')

    def sort_key(p: Path):
        m = FILE_RE.match(p.name)
        if m:
            return (m.group('start'), m.group('end'), p.name)
        return ('9999-99-99', '9999-99-99', p.name)

    return sorted(files, key=sort_key)


def month_to_int(month_token: str) -> int:
    if not re.fullmatch(r'\d{4}-\d{2}', month_token):
        raise SystemExit(f'Bad month token: {month_token!r}. Expected YYYY-MM')
    y, m = month_token.split('-')
    return int(y) * 12 + int(m)


def parse_file_meta(path: Path) -> Optional[dict]:
    m = FILE_RE.match(path.name)
    if not m:
        return None
    start_ms = parse_dt_to_ms_utc(m.group('start') + ' 00:00:00')
    end_ms_excl = parse_dt_to_ms_utc(m.group('end') + ' 00:00:00') + 24 * 3600 * 1000
    return {
        'path': path,
        'pair': m.group('pair'),
        'exchange': m.group('exchange'),
        'start_ms': start_ms,
        'end_ms_excl': end_ms_excl,
        'month': m.group('month'),
    }


def derive_market_symbol(
    ticks_dir: Path,
    files: Sequence[Path],
    symbol_format: str,
    explicit_market_symbol: Optional[str],
) -> str:
    if explicit_market_symbol:
        return explicit_market_symbol
    candidates = [ticks_dir.name] + [p.name for p in files[:3]]
    for text in candidates:
        m = FILE_RE.match(Path(text).name)
        if not m:
            continue
        pair = m.group('pair').upper()
        for quote in ('USDT', 'USDC', 'BTC', 'ETH'):
            if pair.endswith(quote) and len(pair) > len(quote):
                base = pair[:-len(quote)]
                if symbol_format == 'usdtm':
                    return f'{base}/{quote}:{quote}'
                if symbol_format == 'usdt':
                    return f'{base}/{quote}'
                if symbol_format == 'raw':
                    return pair
                raise SystemExit(f'Unsupported local tick symbol format: {symbol_format}')
    raise SystemExit('Could not derive market symbol from folder/files. Pass --market-symbol explicitly.')


def filter_local_tick_files(
    files: Sequence[Path],
    *,
    exact_months: Optional[set[str]],
    month_from: Optional[str],
    month_to: Optional[str],
    start_ms: Optional[int],
    end_ms_excl: Optional[int],
) -> List[Path]:
    out: List[Path] = []
    month_from_i = month_to_int(month_from) if month_from else None
    month_to_i = month_to_int(month_to) if month_to else None

    for p in files:
        meta = parse_file_meta(p)
        if meta is not None:
            month_token = meta['month']
            month_i = month_to_int(month_token)
            if exact_months and month_token not in exact_months:
                continue
            if month_from_i is not None and month_i < month_from_i:
                continue
            if month_to_i is not None and month_i > month_to_i:
                continue
            if start_ms is not None and meta['end_ms_excl'] <= start_ms:
                continue
            if end_ms_excl is not None and meta['start_ms'] >= end_ms_excl:
                continue
        out.append(p)

    if not out:
        raise SystemExit('No local tick files remain after month/date filtering')
    return out


def iter_tick_rows(
    files: Iterable[Path],
    *,
    start_ms: Optional[int],
    end_ms_excl: Optional[int],
) -> Iterator[Tuple[int, float, float]]:
    for path in files:
        log(f'[ticks] read {path}')
        with path.open('r', encoding='utf-8') as fh:
            count = 0
            for line_no, line in enumerate(fh, start=1):
                s = line.strip()
                if not s:
                    continue
                try:
                    obj = json.loads(s)
                except json.JSONDecodeError as exc:
                    raise SystemExit(f'JSON parse error in {path}:{line_no}: {exc}') from exc
                ts = obj.get('timestamp')
                price = obj.get('price')
                volume = obj.get('volume')
                if ts is None or price is None or volume is None:
                    continue
                try:
                    ts_i = int(ts)
                    price_f = float(price)
                    volume_f = float(volume)
                except Exception as exc:
                    raise SystemExit(f'Bad numeric value in {path}:{line_no}: {obj}') from exc
                if start_ms is not None and ts_i < start_ms:
                    continue
                if end_ms_excl is not None and ts_i >= end_ms_excl:
                    continue
                count += 1
                if count % 200000 == 0:
                    log(f'[ticks] {path.name} parsed={count}')
                yield ts_i, price_f, volume_f
            log(f'[ticks] {path.name} done parsed={count}')


def aggregate_ticks_to_bars_stream(
    tick_iter: Iterable[Tuple[int, float, float]],
    tf_ms: int,
) -> pd.DataFrame:
    rows: List[Tuple[str, float, float, float, float, float]] = []
    current_bucket: Optional[int] = None
    o = h = l = c = v = None
    count_ticks = 0
    count_bars = 0

    def flush(bucket_ms: int, open_: float, high_: float, low_: float, close_: float, vol_: float) -> None:
        nonlocal count_bars
        dt = pd.to_datetime(bucket_ms, unit='ms', utc=True).strftime('%Y-%m-%dT%H:%M:%S+00:00')
        rows.append((dt, open_, high_, low_, close_, vol_))
        count_bars += 1
        if count_bars % 100000 == 0:
            log(f'[agg ticks] bars={count_bars}')

    for ts, price, volume in tick_iter:
        count_ticks += 1
        bucket = (ts // tf_ms) * tf_ms
        if current_bucket is None:
            current_bucket = bucket
            o = h = l = c = price
            v = volume
            continue
        if bucket != current_bucket:
            flush(current_bucket, o, h, l, c, v)
            current_bucket = bucket
            o = h = l = c = price
            v = volume
            continue
        if price > h:
            h = price
        if price < l:
            l = price
        c = price
        v += volume

    if current_bucket is not None:
        flush(current_bucket, o, h, l, c, v)

    if not rows:
        return pd.DataFrame(columns=['open', 'high', 'low', 'close', 'volume'])

    df = pd.DataFrame(rows, columns=['datetime_utc', 'open', 'high', 'low', 'close', 'volume'])
    df = df.set_index('datetime_utc')
    log(f'[agg ticks] ticks={count_ticks} bars={count_bars}')
    return df.astype(float)


# -------- row/npz helpers --------

KNOWN_OPTIONAL_COLUMNS = TREND_EXTRA_COLUMNS


def df_to_rows(df: pd.DataFrame, symbol: str, include_trend_columns: bool) -> List[dict]:
    rows: List[dict] = []
    cols = ['open', 'high', 'low', 'close', 'volume'] + BASE_EXTRA_COLUMNS + (TREND_EXTRA_COLUMNS if include_trend_columns else [])
    for idx, r in df.iterrows():
        row = {'symbol': symbol, 'datetime_utc': idx}
        for c in cols:
            val = r.get(c, 0.0)
            if pd.isna(val):
                val = 0.0
            row[c] = float(val)
        rows.append(row)
    return rows


def append_npz_parts(parts: Dict[str, list], symbol: str, df: pd.DataFrame) -> None:
    ts = _datetime_index_epoch_s(df.index)
    parts['symbols'].append(symbol)
    parts['offsets'].append(parts['offsets'][-1] + len(df))
    parts['timestamp_s'].append(ts.astype(np.int64))
    for col in ['open', 'high', 'low', 'close', 'volume'] + KNOWN_OPTIONAL_COLUMNS:
        if col in df.columns:
            parts.setdefault(col, []).append(df[col].astype('float64').to_numpy())


def write_npz(npz_path: str, parts: Dict[str, list]) -> None:
    out = {
        'symbols': np.asarray(parts['symbols'], dtype=object),
        'offsets': np.asarray(parts['offsets'], dtype=np.int64),
        'timestamp_s': np.concatenate(parts['timestamp_s']).astype(np.int64) if parts['timestamp_s'] else np.asarray([], dtype=np.int64),
    }
    for col in ['open', 'high', 'low', 'close', 'volume'] + KNOWN_OPTIONAL_COLUMNS:
        series_parts = parts.get(col) or []
        if series_parts:
            out[col] = np.concatenate(series_parts).astype(np.float64)
    Path(npz_path).parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(npz_path, **out)
    info(f'[npz] wrote {npz_path} symbols={len(parts["symbols"])} rows={int(parts["offsets"][-1])}')


# -------- source selection / processors --------

def choose_source(args, tf_seconds: int) -> str:
    if args.source != 'auto':
        return args.source
    if args.ticks_dir:
        return 'local_ticks'
    if tf_seconds < 60:
        return 'trades_api'
    return 'ohlcv_api'


def build_market_df_from_source(ex, market: str, tf: str, args, start_ms: Optional[int], end_ms: int) -> pd.DataFrame:
    source = choose_source(args, timeframe_to_seconds(tf))
    if source == 'ohlcv_api':
        if start_ms is not None:
            return fetch_ohlcv_range(ex, market, tf, start_ms, end_ms)
        log(f'[fetch ohlcv] {market} one_shot limit={args.limit}')
        return df_from_ohlcv(ex.fetch_ohlcv(market, timeframe=tf, limit=int(args.limit)))
    if source == 'trades_api':
        if start_ms is None:
            if args.back_bars:
                start_ms = end_ms - int(args.back_bars) * timeframe_to_milliseconds(tf)
            else:
                raise SystemExit('trades_api mode requires --start/--end or --back-bars')
        trades_df = fetch_trades_range(
            ex,
            market,
            start_ms,
            end_ms,
            limit_per_call=args.trade_limit_per_call,
            max_empty_tries=args.max_empty_tries,
            sleep_sec=args.sleep_sec,
        )
        if trades_df.empty:
            return pd.DataFrame()
        return aggregate_trades_to_bars(trades_df, tf)
    raise ValueError(f'Unsupported source for per-market API build: {source}')


def build_local_ticks_outputs(args, tf: str, tf_seconds: int, npz_parts: Dict[str, list]) -> None:
    ticks_dir = Path(args.ticks_dir).resolve()
    if not ticks_dir.exists() or not ticks_dir.is_dir():
        raise SystemExit(f'--ticks-dir is not a directory: {ticks_dir}')

    all_files = discover_files(ticks_dir)
    exact_months = None
    if args.months.strip():
        exact_months = {m.strip() for m in args.months.split(',') if m.strip()}
        for m in exact_months:
            month_to_int(m)

    start_ms = parse_dt_to_ms_utc(args.start_utc) if args.start_utc else None
    end_ms = parse_dt_to_ms_utc(args.end_utc) if args.end_utc else None
    if start_ms is not None and end_ms is not None and start_ms >= end_ms:
        raise SystemExit('--start must be earlier than --end')

    files = filter_local_tick_files(
        all_files,
        exact_months=exact_months,
        month_from=args.month_from.strip() or None,
        month_to=args.month_to.strip() or None,
        start_ms=start_ms,
        end_ms_excl=end_ms,
    )
    market = derive_market_symbol(
        ticks_dir=ticks_dir,
        files=files,
        symbol_format=args.local_ticks_symbol_format,
        explicit_market_symbol=args.market_symbol or None,
    )

    log(f'[source] local_ticks ticks_dir={ticks_dir}')
    log(f'[source] local_ticks files_total={len(all_files)} files_selected={len(files)} market={market}')

    df = aggregate_ticks_to_bars_stream(
        iter_tick_rows(files, start_ms=start_ms, end_ms_excl=end_ms),
        timeframe_to_milliseconds(tf),
    )
    if df.empty:
        raise SystemExit('No bars built from local ticks')
    log(f'[bars] {market} rows={len(df)} range={df.index[0]}..{df.index[-1]}')

    work = compute_base_features(df, tf_seconds=tf_seconds, feature_set=args.feature_set)
    if args.cache_pack_trend:
        log(f'[trend] {market} computing pack-trend cache ...')
        work = compute_pack_trend_features(
            work,
            trend_tf=args.trend_ma_tf,
            trend_ma_len=args.trend_ma_len,
            trend_slope_bars=args.trend_slope_bars,
            trend_slope_long_bound_pct=args.trend_slope_long_bound_pct,
            trend_slope_short_bound_pct=args.trend_slope_short_bound_pct,
            trend_score_min_pct=args.trend_score_min_pct,
            trend_score_max_pct=args.trend_score_max_pct,
            min_long_invest_pct=args.min_long_invest_pct,
            max_long_invest_pct=args.max_long_invest_pct,
            min_short_invest_pct=args.min_short_invest_pct,
            max_short_invest_pct=args.max_short_invest_pct,
        )

    if args.db_out:
        rows = df_to_rows(work, symbol=market, include_trend_columns=args.cache_pack_trend)
        insert_rows(args.db_out, rows, include_trend_columns=args.cache_pack_trend)
        log(f'[db] {market} wrote rows={len(rows)} -> {args.db_out}')
    if args.npz_out:
        append_npz_parts(npz_parts, market, work)
        log(f'[npz] {market} queued rows={len(work)}')


# -------- main --------

def main() -> None:
    global ARGS
    ap = argparse.ArgumentParser(description='Fetch/build standard SQLite DB and/or fast NPZ from OHLCV API, trade API, or local ticks')
    ap.add_argument('-i', '--input-csv', '--universe-file', dest='input_csv', default='')
    ap.add_argument('-t', '--timeframe', default='1m')
    ap.add_argument('--source', choices=['auto', 'ohlcv_api', 'trades_api', 'local_ticks'], default='auto')
    ap.add_argument('--limit', type=int, default=1000, help='Legacy one-shot OHLCV fetch limit when no range/back-bars is given')
    ap.add_argument('--debug', action='store_true', help='Verbose progress output for long runs')
    ap.add_argument('--start', dest='start_utc', default='')
    ap.add_argument('--end', dest='end_utc', default='')
    ap.add_argument('--back-bars', dest='back_bars', type=int, default=0)
    ap.add_argument('--exchange', default='bingx')
    ap.add_argument('--ccxt-symbol-format', choices=['auto', 'usdtm', 'usdt', 'spot_only', 'perp_only'], default='usdtm')
    ap.add_argument('--trade-limit-per-call', type=int, default=1000)
    ap.add_argument('--sleep-sec', type=float, default=0.15)
    ap.add_argument('--max-empty-tries', type=int, default=3)
    ap.add_argument('--max-retries', type=int, default=8)
    ap.add_argument('--db-out', '--output', dest='db_out', default='')
    ap.add_argument('--npz-out', dest='npz_out', default='')
    ap.add_argument('--npz-only', action='store_true', help='Generate NPZ and skip SQLite DB entirely')
    ap.add_argument('--fresh', action='store_true')
    ap.add_argument('--feature-set', choices=['full', 'none'], default='full', help='full = old extra indicators, none = OHLCV only + zeros in schema fields')
    ap.add_argument('--cache-pack-trend', action='store_true', help='Cache current pack-strategy trend target columns')
    ap.add_argument('--trend-ma-tf', default='W')
    ap.add_argument('--trend-ma-len', type=int, default=20)
    ap.add_argument('--trend-slope-bars', type=int, default=3)
    ap.add_argument('--trend-slope-long-bound-pct', type=float, default=1.0)
    ap.add_argument('--trend-slope-short-bound-pct', type=float, default=-1.0)
    ap.add_argument('--trend-score-min-pct', type=float, default=45.0)
    ap.add_argument('--trend-score-max-pct', type=float, default=75.0)
    ap.add_argument('--min-long-invest-pct', type=float, default=0.5)
    ap.add_argument('--max-long-invest-pct', type=float, default=2.0)
    ap.add_argument('--min-short-invest-pct', type=float, default=0.5)
    ap.add_argument('--max-short-invest-pct', type=float, default=2.0)

    # local tick mode
    ap.add_argument('--ticks-dir', default='', help='Folder with local monthly tick JSONL files')
    ap.add_argument('--market-symbol', default='', help='Exact market symbol to store, e.g. ENA/USDT:USDT')
    ap.add_argument('--local-ticks-symbol-format', choices=['usdtm', 'usdt', 'raw'], default='usdtm')
    ap.add_argument('--months', default='', help='Comma-separated exact months for local ticks, e.g. 2025-03,2025-04')
    ap.add_argument('--month-from', default='', help='Local ticks month lower bound inclusive, YYYY-MM')
    ap.add_argument('--month-to', default='', help='Local ticks month upper bound inclusive, YYYY-MM')
    args = ap.parse_args()
    ARGS = args

    global DEBUG
    DEBUG = bool(args.debug)

    if args.npz_only:
        args.db_out = ''
    if not args.db_out and not args.npz_out:
        raise SystemExit('At least one output is required: --db-out and/or --npz-out')

    tf = normalize_timeframe(args.timeframe)
    tf_seconds = timeframe_to_seconds(tf)
    source = choose_source(args, tf_seconds)
    log(f'[cfg] timeframe={tf} tf_seconds={tf_seconds} source={source}')

    if args.db_out:
        Path(args.db_out).parent.mkdir(parents=True, exist_ok=True)
        ensure_schema(args.db_out, include_trend_columns=args.cache_pack_trend)
        if args.fresh:
            con = sqlite3.connect(args.db_out)
            cur = con.cursor()
            cur.execute('DROP TABLE IF EXISTS price_indicators')
            con.commit()
            con.close()
            ensure_schema(args.db_out, include_trend_columns=args.cache_pack_trend)
            info(f'[db] fresh reset -> {args.db_out}')

    npz_parts: Dict[str, list] = {'symbols': [], 'offsets': [0], 'timestamp_s': []}

    if source == 'local_ticks':
        build_local_ticks_outputs(args, tf, tf_seconds, npz_parts)
    else:
        if ccxt is None:
            raise SystemExit('ccxt is required: pip install ccxt')
        if not args.input_csv:
            raise SystemExit('--input-csv / --universe-file is required for exchange API modes')

        ex = getattr(ccxt, args.exchange)({'enableRateLimit': True})
        log(f'[exchange] loading markets for {args.exchange} ...')
        ex.load_markets()
        log(f'[exchange] markets_loaded={len(getattr(ex, "markets", {}) or {})}')

        syms = load_universe_symbols(args.input_csv)
        log(f'[universe] symbols={len(syms)} file={args.input_csv}')

        end_ms = parse_dt_to_ms_utc(args.end_utc) if args.end_utc else int(pd.Timestamp.utcnow().value // 10**6)
        start_ms = parse_dt_to_ms_utc(args.start_utc) if args.start_utc else None
        if args.back_bars:
            start_ms = end_ms - int(args.back_bars) * timeframe_to_milliseconds(tf)

        for i, raw in enumerate(syms, start=1):
            market = resolve_market(ex, raw, fmt_bias=args.ccxt_symbol_format)
            if not market:
                info(f'[skip] {i}/{len(syms)} {raw} unresolved', file=sys.stderr)
                continue
            try:
                log(f'[symbol] {i}/{len(syms)} raw={raw} market={market}')
                df = build_market_df_from_source(ex, market, tf, args, start_ms, end_ms)
                if df.empty:
                    info(f'[skip] {i}/{len(syms)} {market} empty', file=sys.stderr)
                    continue
                log(f'[bars] {market} rows={len(df)} range={df.index[0]}..{df.index[-1]}')
                work = compute_base_features(df, tf_seconds=tf_seconds, feature_set=args.feature_set)
                if args.cache_pack_trend:
                    log(f'[trend] {market} computing pack-trend cache ...')
                    work = compute_pack_trend_features(
                        work,
                        trend_tf=args.trend_ma_tf,
                        trend_ma_len=args.trend_ma_len,
                        trend_slope_bars=args.trend_slope_bars,
                        trend_slope_long_bound_pct=args.trend_slope_long_bound_pct,
                        trend_slope_short_bound_pct=args.trend_slope_short_bound_pct,
                        trend_score_min_pct=args.trend_score_min_pct,
                        trend_score_max_pct=args.trend_score_max_pct,
                        min_long_invest_pct=args.min_long_invest_pct,
                        max_long_invest_pct=args.max_long_invest_pct,
                        min_short_invest_pct=args.min_short_invest_pct,
                        max_short_invest_pct=args.max_short_invest_pct,
                    )
                if args.db_out:
                    rows = df_to_rows(work, symbol=market, include_trend_columns=args.cache_pack_trend)
                    insert_rows(args.db_out, rows, include_trend_columns=args.cache_pack_trend)
                    log(f'[db] {market} wrote rows={len(rows)} -> {args.db_out}')
                if args.npz_out:
                    append_npz_parts(npz_parts, market, work)
                    log(f'[npz] {market} queued rows={len(work)}')
            except Exception as e:
                info(f'[err] {i}/{len(syms)} {market}: {e}', file=sys.stderr)

    if args.npz_out and npz_parts['symbols']:
        write_npz(args.npz_out, npz_parts)
    info('[done] complete')


if __name__ == '__main__':
    main()
