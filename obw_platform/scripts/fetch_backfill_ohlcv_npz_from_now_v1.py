#!/usr/bin/env python3
"""Backfill OHLCV NPZ from newest bars backwards.

This is meant for newly listed symbols where querying from "one year ago"
returns an empty response or exchange-specific range errors. It starts from the
latest OHLCV batch, then walks backward in fixed-size windows until it reaches
the requested lookback or no older data is available.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Dict, List, Sequence

import ccxt
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "obw_platform") not in sys.path:
    sys.path.insert(0, str(ROOT / "obw_platform"))

from fetch_build_cache_and_fast_v1 import (  # noqa: E402
    append_npz_parts,
    compute_base_features,
    compute_pack_trend_features,
    normalize_timeframe,
    resolve_market,
    timeframe_to_milliseconds,
    write_npz,
)


def load_symbols(path: str) -> List[str]:
    out: List[str] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


def df_from_rows(rows: Sequence[Sequence[float]]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.set_index("ts").sort_index()


def fetch_backwards(ex, market: str, timeframe: str, back_bars: int, limit: int, sleep_sec: float) -> pd.DataFrame:
    tf_ms = timeframe_to_milliseconds(timeframe)
    batches: List[pd.DataFrame] = []
    seen_earliest_ms = None
    target_start_ms = int(pd.Timestamp.utcnow().value // 10**6) - int(back_bars) * tf_ms
    empty_or_stuck = 0

    while True:
        if seen_earliest_ms is None:
            since = None
        else:
            since = max(target_start_ms, int(seen_earliest_ms) - int(limit) * tf_ms)

        rows = ex.fetch_ohlcv(market, timeframe=timeframe, since=since, limit=limit)
        if not rows:
            empty_or_stuck += 1
            if empty_or_stuck >= 3:
                break
            if seen_earliest_ms is None:
                break
            seen_earliest_ms = max(target_start_ms, int(seen_earliest_ms) - int(limit) * tf_ms)
            time.sleep(sleep_sec)
            continue

        df = df_from_rows(rows)
        if seen_earliest_ms is not None:
            df = df[df.index.view("int64") // 10**6 < int(seen_earliest_ms)]
        if df.empty:
            empty_or_stuck += 1
            if empty_or_stuck >= 3:
                break
        else:
            empty_or_stuck = 0
            batches.append(df)
            earliest_ms = int(df.index[0].value // 10**6)
            latest_ms = int(df.index[-1].value // 10**6)
            print(
                f"[batch] {market} rows={len(df)} range={df.index[0]}..{df.index[-1]} "
                f"total_batches={len(batches)}",
                flush=True,
            )
            if earliest_ms <= target_start_ms:
                break
            if seen_earliest_ms is not None and earliest_ms >= int(seen_earliest_ms):
                break
            seen_earliest_ms = earliest_ms
            if sum(len(x) for x in batches) >= int(back_bars):
                break

        time.sleep(sleep_sec)

    if not batches:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    out = pd.concat(batches).sort_index()
    out = out[~out.index.duplicated(keep="first")]
    if len(out) > int(back_bars):
        out = out.iloc[-int(back_bars):]
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill OHLCV from newest bars backwards and write fast NPZ")
    ap.add_argument("-i", "--input-csv", required=True)
    ap.add_argument("-t", "--timeframe", default="1m")
    ap.add_argument("--back-bars", type=int, default=525600)
    ap.add_argument("--exchange", default="bingx")
    ap.add_argument("--ccxt-symbol-format", choices=["auto", "usdtm", "usdt", "spot_only", "perp_only"], default="usdtm")
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--sleep-sec", type=float, default=0.2)
    ap.add_argument("--npz-out", required=True)
    ap.add_argument("--npz-only", action="store_true", help="Accepted for compatibility with fetch_build_cache_and_fast_v1.py")
    ap.add_argument("--debug", action="store_true", help="Accepted for compatibility; this script is already verbose")
    ap.add_argument("--feature-set", choices=["full", "none"], default="none")
    ap.add_argument("--cache-pack-trend", action="store_true")
    ap.add_argument("--trend-ma-tf", default="W")
    ap.add_argument("--trend-ma-len", type=int, default=20)
    ap.add_argument("--trend-slope-bars", type=int, default=3)
    ap.add_argument("--trend-slope-long-bound-pct", type=float, default=1.0)
    ap.add_argument("--trend-slope-short-bound-pct", type=float, default=-1.0)
    ap.add_argument("--trend-score-min-pct", type=float, default=45.0)
    ap.add_argument("--trend-score-max-pct", type=float, default=75.0)
    ap.add_argument("--min-long-invest-pct", type=float, default=0.5)
    ap.add_argument("--max-long-invest-pct", type=float, default=2.0)
    ap.add_argument("--min-short-invest-pct", type=float, default=0.5)
    ap.add_argument("--max-short-invest-pct", type=float, default=2.0)
    args = ap.parse_args()

    tf = normalize_timeframe(args.timeframe)
    tf_seconds = timeframe_to_milliseconds(tf) // 1000
    ex = getattr(ccxt, args.exchange)({"enableRateLimit": True})
    print(f"[exchange] loading markets for {args.exchange}", flush=True)
    ex.load_markets()
    print(f"[exchange] markets_loaded={len(getattr(ex, 'markets', {}) or {})}", flush=True)

    parts: Dict[str, list] = {"symbols": [], "offsets": [0], "timestamp_s": []}
    for raw in load_symbols(args.input_csv):
        market = resolve_market(ex, raw, fmt_bias=args.ccxt_symbol_format)
        if not market:
            print(f"[skip] {raw} unresolved", file=sys.stderr, flush=True)
            continue
        print(f"[symbol] raw={raw} market={market}", flush=True)
        df = fetch_backwards(ex, market, tf, args.back_bars, args.limit, args.sleep_sec)
        if df.empty:
            print(f"[skip] {market} empty", file=sys.stderr, flush=True)
            continue
        work = compute_base_features(df, tf_seconds=tf_seconds, feature_set=args.feature_set)
        if args.cache_pack_trend:
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
        append_npz_parts(parts, market, work)
        print(f"[npz] queued {market} rows={len(work)} range={work.index[0]}..{work.index[-1]}", flush=True)

    if len(parts["symbols"]) == 0:
        raise SystemExit("No valid symbols fetched")
    write_npz(args.npz_out, parts)
    print(f"[done] wrote {args.npz_out}", flush=True)


if __name__ == "__main__":
    main()
