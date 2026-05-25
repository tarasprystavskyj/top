#!/usr/bin/env python3
from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import List, Optional

import numpy as np

try:
    import ccxt  # type: ignore
except Exception as exc:
    raise SystemExit(f"ccxt is required: {exc}")


def read_symbols(path: Path) -> List[str]:
    out: List[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


def resolve_market(ex, raw: str) -> Optional[str]:
    raw = raw.strip().upper()
    markets = ex.markets or ex.load_markets()
    if raw in markets:
        return raw
    base = raw.split("/")[0].split(":")[0]
    if base.endswith("USDT") and len(base) > 4:
        base = base[:-4]
    for candidate in (f"{base}/USDT:USDT", f"{base}/USDT", f"{base}/USDC:USDC", f"{base}/USDC"):
        if candidate in markets:
            return candidate
    return None


def ms(dt: str) -> int:
    return int(np.datetime64(dt).astype("datetime64[ms]").astype(np.int64))


def fetch_symbol(ex, market: str, start_ms: int, end_ms: int, sleep_sec: float, max_retries: int) -> np.ndarray:
    cursor = start_ms
    rows: List[List[float]] = []
    req = 0
    while cursor < end_ms:
        req += 1
        retries = 0
        while True:
            try:
                batch = ex.fetch_ohlcv(market, timeframe="1m", since=cursor, limit=1000)
                break
            except Exception as exc:
                retries += 1
                if retries > max_retries:
                    print(f"[err] {market} req={req} cursor={cursor} {exc}", flush=True)
                    return np.asarray(rows, dtype=np.float64)
                wait = max(2.0, sleep_sec * 8.0) * retries
                print(f"[retry] {market} req={req} retry={retries} wait={wait:.1f}s err={exc}", flush=True)
                time.sleep(wait)
        if not batch:
            print(f"[stop] {market} empty req={req}", flush=True)
            break
        kept = [r for r in batch if start_ms <= int(r[0]) < end_ms]
        rows.extend(kept)
        last = int(batch[-1][0])
        next_cursor = last + 60_000
        print(f"[fetch] {market} req={req} rows={len(kept)} last={np.datetime64(last, 'ms')} total={len(rows)}", flush=True)
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(batch) < 1000:
            break
        time.sleep(sleep_sec)
    if not rows:
        return np.empty((0, 6), dtype=np.float64)
    arr = np.asarray(rows, dtype=np.float64)
    _, unique_idx = np.unique(arr[:, 0].astype(np.int64), return_index=True)
    return arr[np.sort(unique_idx)]


def main() -> None:
    ap = argparse.ArgumentParser(description="Collect 1m OHLCV NPZ candidates with throttling.")
    ap.add_argument("--universe", default="obw_platform/universe/universe_ena_second_leg_candidates.txt")
    ap.add_argument("--exchange", default="bybit")
    ap.add_argument("--start", default="2025-03-01T00:00:00")
    ap.add_argument("--end", default="2026-03-02T00:00:00")
    ap.add_argument("--out", default="DB/ohlcv_1m_ena_second_leg_candidates_1y.npz")
    ap.add_argument("--sleep-sec", type=float, default=0.35)
    ap.add_argument("--max-retries", type=int, default=8)
    args = ap.parse_args()

    ex = getattr(ccxt, args.exchange)({"enableRateLimit": True})
    ex.load_markets()
    start_ms = ms(args.start)
    end_ms = ms(args.end)
    symbols = read_symbols(Path(args.universe))

    out_symbols: List[str] = []
    offsets: List[int] = []
    arrays = {"timestamp_s": [], "open": [], "high": [], "low": [], "close": [], "volume": []}
    cursor_rows = 0
    for raw in symbols:
        market = resolve_market(ex, raw)
        if not market:
            print(f"[skip] {raw} unresolved", flush=True)
            continue
        print(f"[symbol] raw={raw} market={market}", flush=True)
        arr = fetch_symbol(ex, market, start_ms, end_ms, args.sleep_sec, args.max_retries)
        if arr.size == 0:
            print(f"[skip] {market} no rows", flush=True)
            continue
        offsets.append(cursor_rows)
        out_symbols.append(market)
        arrays["timestamp_s"].append((arr[:, 0] // 1000).astype(np.int64))
        arrays["open"].append(arr[:, 1].astype(np.float64))
        arrays["high"].append(arr[:, 2].astype(np.float64))
        arrays["low"].append(arr[:, 3].astype(np.float64))
        arrays["close"].append(arr[:, 4].astype(np.float64))
        arrays["volume"].append(arr[:, 5].astype(np.float64))
        cursor_rows += len(arr)
        print(f"[queued] {market} rows={len(arr)}", flush=True)

    if not out_symbols:
        raise SystemExit("No symbols collected")
    merged = {k: np.concatenate(v) for k, v in arrays.items()}
    close = merged["close"]
    volume = merged["volume"]
    zeros = np.zeros_like(close, dtype=np.float64)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        out,
        symbols=np.asarray(out_symbols),
        offsets=np.asarray(offsets, dtype=np.int64),
        timestamp_s=merged["timestamp_s"],
        open=merged["open"],
        high=merged["high"],
        low=merged["low"],
        close=close,
        volume=volume,
        atr_ratio=zeros,
        gain_24h_before=zeros,
        dp6h=zeros,
        dp12h=zeros,
        quote_volume=close * volume,
        qv_24h=zeros,
        vol_surge_mult=zeros,
    )
    print(f"[done] out={out} symbols={len(out_symbols)} rows={len(close)}", flush=True)


if __name__ == "__main__":
    main()
