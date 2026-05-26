#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetch linear futures OHLCV via CCXT and write compact multi-symbol NPZ.

NPZ format, compatible with old NumPy without pickle:
  symbols: unicode array, len=N
  offsets: int64, len=N+1
  timestamp_s, open, high, low, close, volume: concatenated arrays

Example:
  python obw_platform/telegram_signal_tools/fetch_futures_ohlcv_npz_v1.py \
    --exchange bingx \
    --universe-file universe/telegram_signal_universe_all.txt \
    --timeframe 3m \
    --bars 7200 \
    --out DB/telegram_signals_3m_7200b.npz
"""
import argparse
import math
import sys
from datetime import datetime, timedelta, timezone
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np

try:
    import ccxt  # type: ignore
except Exception:
    ccxt = None


def normalize_token(raw: str) -> str:
    return str(raw).strip().upper()


def load_universe(path: str) -> List[str]:
    out: List[str] = []
    seen = set()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.lower() in {"symbol", "symbols"}:
            continue
        s = normalize_token(s)
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def base_quote(raw: str) -> Tuple[str, Optional[str]]:
    s = normalize_token(raw)
    if "/" in s:
        base, rest = s.split("/", 1)
        quote = rest.split(":", 1)[0]
        return base, quote
    for q in ("USDT", "USDC", "USD"):
        if s.endswith(q) and len(s) > len(q):
            return s[:-len(q)], q
    return s, None


def build_exchange(name: str):
    if ccxt is None:
        raise SystemExit("ccxt is required. Install inside venv: pip install ccxt")
    cls = getattr(ccxt, name)
    ex = cls({"enableRateLimit": True})
    ex.load_markets()
    return ex


def list_linear_futures(ex, quotes: Iterable[str]) -> List[str]:
    quote_set = {q.upper() for q in quotes}
    out = []
    for sym, m in ex.markets.items():
        if not m.get("active", True):
            continue
        if not (m.get("swap") or m.get("future")):
            continue
        if m.get("linear") is False:
            continue
        q = str(m.get("quote") or "").upper()
        if quote_set and q not in quote_set:
            continue
        out.append(sym)
    return sorted(set(out))


def resolve_market(ex, raw: str, quote_priority: Tuple[str, ...] = ("USDT", "USDC")) -> Optional[str]:
    s = normalize_token(raw)
    markets = ex.markets
    if s in markets:
        return s
    b, q = base_quote(s)
    quotes = (q,) + tuple(x for x in quote_priority if x != q) if q else quote_priority
    candidates: List[str] = []
    for quote in quotes:
        candidates.extend([
            f"{b}/{quote}:{quote}",
            f"{b}/{quote}",
            f"{b}{quote}",
        ])
    for c in candidates:
        if c in markets:
            return c
    # fallback by base/quote metadata
    for sym, m in markets.items():
        if str(m.get("base") or "").upper() == b and str(m.get("quote") or "").upper() in quotes:
            if m.get("swap") or m.get("future"):
                if m.get("linear") is not False:
                    return sym
    return None


def timeframe_ms(ex, timeframe: str) -> int:
    try:
        return int(ex.parse_timeframe(timeframe) * 1000)
    except Exception:
        unit = timeframe[-1]
        val = int(timeframe[:-1])
        mult = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}[unit]
        return val * mult


def parse_utc_ms(raw: str) -> Optional[int]:
    s = str(raw or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except AttributeError:
        sign_pos = max(s.rfind("+"), s.rfind("-", 10))
        offset = None
        main = s
        if sign_pos > 10:
            main = s[:sign_pos]
            off = s[sign_pos:]
            sign = 1 if off[0] == "+" else -1
            hh, mm = off[1:].split(":", 1)
            offset = timezone(sign * timedelta(hours=int(hh), minutes=int(mm)))
        try:
            dt = datetime.strptime(main, "%Y-%m-%dT%H:%M:%S.%f")
        except ValueError:
            dt = datetime.strptime(main, "%Y-%m-%dT%H:%M:%S")
        if offset is not None:
            dt = dt.replace(tzinfo=offset)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def fetch_ohlcv_window(
    ex,
    symbol: str,
    timeframe: str,
    bars: int,
    sleep_sec: float,
    max_empty: int = 3,
    since_ms: Optional[int] = None,
    until_ms: Optional[int] = None,
) -> List[List[float]]:
    tf_ms = timeframe_ms(ex, timeframe)
    limit = min(1000, max(10, bars))
    now_ms = int(ex.milliseconds()) if hasattr(ex, "milliseconds") else int(time.time() * 1000)
    # Start slightly earlier to survive missing bars and exchange rounding.
    since = since_ms if since_ms is not None else now_ms - int(bars * tf_ms * 1.25) - 20 * tf_ms
    stop_ms = until_ms if until_ms is not None else now_ms + tf_ms
    rows: List[List[float]] = []
    last_ts: Optional[int] = None
    empty_count = 0

    while len(rows) < bars:
        batch = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=limit)
        if not batch:
            empty_count += 1
            if empty_count >= max_empty:
                break
            since += limit * tf_ms
            if since >= stop_ms:
                break
            time.sleep(sleep_sec)
            continue
        empty_count = 0
        clean = []
        for r in batch:
            ts = int(r[0])
            if last_ts is None or ts > last_ts:
                clean.append(r)
        if not clean:
            break
        rows.extend(clean)
        last_ts = int(clean[-1][0])
        since = last_ts + tf_ms
        if len(clean) < limit and until_ms is None:
            break
        if since >= stop_ms:
            break
        time.sleep(sleep_sec)

    dedup: Dict[int, List[float]] = {}
    for r in rows:
        dedup[int(r[0])] = r
    rows = [dedup[k] for k in sorted(dedup.keys())]
    return rows[-bars:]


def save_npz(out_path: str, by_symbol: Dict[str, List[List[float]]]) -> None:
    symbols = list(by_symbol.keys())
    max_len = max([len(s) for s in symbols] + [1])
    offsets = [0]
    cols = {"timestamp_s": [], "open": [], "high": [], "low": [], "close": [], "volume": []}
    for s in symbols:
        rows = by_symbol[s]
        arr = np.asarray(rows, dtype=np.float64)
        cols["timestamp_s"].append((arr[:, 0] // 1000).astype(np.int64))
        cols["open"].append(arr[:, 1].astype(np.float64))
        cols["high"].append(arr[:, 2].astype(np.float64))
        cols["low"].append(arr[:, 3].astype(np.float64))
        cols["close"].append(arr[:, 4].astype(np.float64))
        cols["volume"].append(arr[:, 5].astype(np.float64))
        offsets.append(offsets[-1] + len(rows))
    data = {
        "symbols": np.asarray(symbols, dtype=f"<U{max_len}"),
        "offsets": np.asarray(offsets, dtype=np.int64),
    }
    for k, parts in cols.items():
        data[k] = np.concatenate(parts) if parts else np.asarray([], dtype=np.float64)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out_path, **data)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exchange", default="bingx", help="ccxt exchange id: bingx, bybit, okx, gateio...")
    ap.add_argument("--universe-file", default="", help="Text file with base symbols or ccxt market symbols")
    ap.add_argument("--out", required=True)
    ap.add_argument("--timeframe", default="3m")
    ap.add_argument("--bars", type=int, default=7200)
    ap.add_argument("--quotes", default="USDT")
    ap.add_argument("--sleep-sec", type=float, default=0.12)
    ap.add_argument("--min-bars", type=int, default=1000)
    ap.add_argument("--max-symbols", type=int, default=0)
    ap.add_argument("--since-utc", default="", help="Optional UTC start, e.g. 2025-11-01T00:00:00Z")
    ap.add_argument("--until-utc", default="", help="Optional UTC end, defaults to exchange now")
    ap.add_argument("--max-empty", type=int, default=3, help="Consecutive empty batches before skipping a symbol")
    args = ap.parse_args()

    ex = build_exchange(args.exchange)
    quotes = tuple(q.strip().upper() for q in args.quotes.split(",") if q.strip())
    if args.universe_file:
        raw_symbols = load_universe(args.universe_file)
        markets = []
        for raw in raw_symbols:
            m = resolve_market(ex, raw, quote_priority=quotes)
            if not m:
                print(f"[skip] unresolved {raw}", file=sys.stderr)
                continue
            markets.append(m)
    else:
        markets = list_linear_futures(ex, quotes)
    markets = sorted(set(markets))
    if args.max_symbols and args.max_symbols > 0:
        markets = markets[: args.max_symbols]
    since_ms = parse_utc_ms(args.since_utc)
    until_ms = parse_utc_ms(args.until_utc)
    print(
        f"[cfg] exchange={args.exchange} timeframe={args.timeframe} bars={args.bars} "
        f"markets={len(markets)} since_utc={args.since_utc or '-'} until_utc={args.until_utc or '-'} "
        f"out={args.out}",
        flush=True,
    )

    by_symbol: Dict[str, List[List[float]]] = {}
    for i, market in enumerate(markets, 1):
        try:
            rows = fetch_ohlcv_window(
                ex,
                market,
                args.timeframe,
                args.bars,
                args.sleep_sec,
                max_empty=args.max_empty,
                since_ms=since_ms,
                until_ms=until_ms,
            )
            if len(rows) < args.min_bars:
                print(f"[skip] {i}/{len(markets)} {market} too_few_bars={len(rows)}", flush=True)
                continue
            by_symbol[market] = rows
            t0 = time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(rows[0][0]) // 1000))
            t1 = time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(rows[-1][0]) // 1000))
            print(f"[ok] {i}/{len(markets)} {market} bars={len(rows)} {t0}..{t1}", flush=True)
        except Exception as e:
            print(f"[err] {i}/{len(markets)} {market}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)

    if not by_symbol:
        raise SystemExit("No valid symbols fetched")
    save_npz(args.out, by_symbol)
    print(f"[done] wrote {args.out} symbols={len(by_symbol)} rows={sum(len(v) for v in by_symbol.values())}", flush=True)


if __name__ == "__main__":
    main()
