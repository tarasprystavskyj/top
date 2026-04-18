#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

try:
    import ccxt  # type: ignore
except Exception:
    ccxt = None


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except Exception:
            pass


def log(*parts, file=None) -> None:
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
    return TF_ALIASES.get(s, tf)


def timeframe_to_seconds(tf: str) -> int:
    tf = normalize_timeframe(tf).strip()
    unit = tf[-1]
    try:
        n = int(tf[:-1])
    except Exception:
        n = 1
    mult = {'m': 60, 'h': 3600, 'd': 86400, 'w': 604800}
    return n * mult.get(unit, 60)


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
    ts = ts.tz_convert('UTC') if ts.tzinfo else ts.tz_localize('UTC')
    tf = str(trend_tf).upper()
    if tf == 'W':
        iso = ts.isocalendar()
        return f'{int(iso.year)}-W{int(iso.week):02d}'
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


def parse_dt_to_ms_utc(s: str) -> int:
    ts = pd.to_datetime(s, utc=True)
    return int(ts.value // 10**6)


def df_from_ohlcv(ohlcv: Sequence[Sequence[float]]) -> pd.DataFrame:
    if not ohlcv:
        return pd.DataFrame()
    df = pd.DataFrame(ohlcv, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
    df['datetime_utc'] = pd.to_datetime(df['ts'], unit='ms', utc=True).dt.strftime('%Y-%m-%dT%H:%M:%S+00:00')
    return df.set_index('datetime_utc')[['open', 'high', 'low', 'close', 'volume']].astype(float)


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
        log(f'[fetch] {market} req={req_no} since={pd.to_datetime(cursor, unit="ms", utc=True)} limit={limit}')
        ohlcv = ex.fetch_ohlcv(market, timeframe=timeframe, since=cursor, limit=limit)
        if not ohlcv:
            log(f'[fetch] {market} empty_batch -> stop')
            break
        df = df_from_ohlcv(ohlcv)
        if df.empty:
            log(f'[fetch] {market} empty_df -> stop')
            break
        frames.append(df)
        last_ts = int(pd.to_datetime(df.index[-1], utc=True).value // 10**6)
        next_cursor = last_ts + tf_ms
        log(f'[fetch] {market} batch_rows={len(df)} last={df.index[-1]} next={pd.to_datetime(next_cursor, unit="ms", utc=True)}')
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(df) < limit:
            break
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames).sort_index()
    out = out[~out.index.duplicated(keep='last')]
    idx_ms = pd.to_datetime(out.index, utc=True).view('int64') // 10**6
    mask = (idx_ms >= start_ms) & (idx_ms < end_ms)
    return out.loc[mask]


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
    ts = pd.to_datetime(df.index, utc=True).astype('int64').to_numpy() // 1_000_000_000
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
    log(f'[npz] wrote {npz_path} symbols={len(parts["symbols"])} rows={int(parts["offsets"][-1])}')


# -------- main --------

def main() -> None:
    ap = argparse.ArgumentParser(description='Fetch OHLCV universe and build SQLite DB and/or fast NPZ in one pass')
    ap.add_argument('-i', '--input-csv', '--universe-file', dest='input_csv', required=True)
    ap.add_argument('-t', '--timeframe', default='1m')
    ap.add_argument('--limit', type=int, default=1000, help='Legacy one-shot fetch limit when no range/back-bars is given')
    ap.add_argument('--start', dest='start_utc', default='')
    ap.add_argument('--end', dest='end_utc', default='')
    ap.add_argument('--back-bars', dest='back_bars', type=int, default=0)
    ap.add_argument('--exchange', default='bingx')
    ap.add_argument('--ccxt-symbol-format', choices=['auto', 'usdtm', 'usdt', 'spot_only', 'perp_only'], default='usdtm')
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
    args = ap.parse_args()

    if ccxt is None:
        raise SystemExit('ccxt is required: pip install ccxt')
    if args.npz_only:
        args.db_out = ''
    if not args.db_out and not args.npz_out:
        raise SystemExit('At least one output is required: --db-out and/or --npz-out')

    tf = normalize_timeframe(args.timeframe)
    tf_seconds = timeframe_to_seconds(tf)
    ex = getattr(ccxt, args.exchange)({'enableRateLimit': True})
    log(f'[exchange] loading markets for {args.exchange} ...')
    ex.load_markets()
    log(f'[exchange] markets_loaded={len(getattr(ex, "markets", {}) or {})}')

    syms = load_universe_symbols(args.input_csv)
    log(f'[universe] symbols={len(syms)} file={args.input_csv}')

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
            log(f'[db] fresh reset -> {args.db_out}')

    npz_parts: Dict[str, list] = {'symbols': [], 'offsets': [0], 'timestamp_s': []}

    end_ms = parse_dt_to_ms_utc(args.end_utc) if args.end_utc else int(pd.Timestamp.utcnow().value // 10**6)
    start_ms = parse_dt_to_ms_utc(args.start_utc) if args.start_utc else None
    if args.back_bars:
        start_ms = end_ms - int(args.back_bars) * timeframe_to_milliseconds(tf)

    for i, raw in enumerate(syms, start=1):
        market = resolve_market(ex, raw, fmt_bias=args.ccxt_symbol_format)
        if not market:
            log(f'[skip] {i}/{len(syms)} {raw} unresolved', file=sys.stderr)
            continue
        try:
            log(f'[symbol] {i}/{len(syms)} raw={raw} market={market}')
            if start_ms is not None:
                df = fetch_ohlcv_range(ex, market, tf, start_ms, end_ms)
            else:
                log(f'[fetch] {market} one_shot limit={args.limit}')
                df = df_from_ohlcv(ex.fetch_ohlcv(market, timeframe=tf, limit=int(args.limit)))
            if df.empty:
                log(f'[skip] {i}/{len(syms)} {market} empty', file=sys.stderr)
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
            log(f'[err] {i}/{len(syms)} {market}: {e}', file=sys.stderr)

    if args.npz_out and npz_parts['symbols']:
        write_npz(args.npz_out, npz_parts)
    log('[done] complete')


if __name__ == '__main__':
    main()
