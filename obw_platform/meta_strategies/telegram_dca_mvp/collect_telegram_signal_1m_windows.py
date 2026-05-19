#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Collect 1m futures OHLCV windows for static Telegram signals.

Research/data-collection only. This script does not import live runners,
daemons, broker clients, or order execution code.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np

try:
    import ccxt  # type: ignore
except Exception:
    ccxt = None


ROOT = Path(__file__).resolve().parents[3]
OHLCV_COLS = ("timestamp_s", "open", "high", "low", "close", "volume")


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def parse_iso_utc(raw: str) -> dt.datetime:
    value = dt.datetime.fromisoformat(str(raw).strip())
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def floor_minute(value: dt.datetime) -> dt.datetime:
    return value.replace(second=0, microsecond=0)


def ceil_minute(value: dt.datetime) -> dt.datetime:
    floored = floor_minute(value)
    if floored == value:
        return floored
    return floored + dt.timedelta(minutes=1)


def dt_to_ms(value: dt.datetime) -> int:
    return int(value.timestamp() * 1000)


def ms_to_iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).replace(microsecond=0).isoformat()


def s_to_iso(sec: int) -> str:
    return dt.datetime.fromtimestamp(sec, tz=dt.timezone.utc).replace(microsecond=0).isoformat()


def normalize_token(raw: str) -> str:
    return str(raw).strip().upper()


def load_universe(path: Path) -> List[str]:
    out: List[str] = []
    seen = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        s = normalize_token(line)
        if not s or s.startswith("#") or s.lower() in {"symbol", "symbols"}:
            continue
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def inspect_signal_csv(path: Path) -> Dict[str, Any]:
    rows = []
    with path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    if not rows:
        raise SystemExit(f"empty signal csv: {path}")
    dts = [parse_iso_utc(r["dt_utc"]) for r in rows if r.get("dt_utc")]
    symbols = sorted({normalize_token(r["symbol"]) for r in rows if r.get("symbol")})
    if not dts:
        raise SystemExit(f"no dt_utc values in signal csv: {path}")
    return {
        "rows": len(rows),
        "dt_min": min(dts),
        "dt_max": max(dts),
        "symbols": symbols,
    }


def timeframe_ms(ex: Any, timeframe: str) -> int:
    try:
        return int(ex.parse_timeframe(timeframe) * 1000)
    except Exception:
        unit = timeframe[-1]
        val = int(timeframe[:-1])
        return val * {"m": 60_000, "h": 3_600_000, "d": 86_400_000}[unit]


def build_exchange(exchange_id: str) -> Any:
    if ccxt is None:
        raise SystemExit("ccxt is required: pip install ccxt")
    cls = getattr(ccxt, exchange_id)
    ex = cls({"enableRateLimit": True})
    ex.load_markets()
    return ex


def resolve_linear_futures_market(ex: Any, raw: str, quotes: Iterable[str]) -> Optional[str]:
    base = normalize_token(raw)
    quotes_tuple = tuple(q.upper() for q in quotes)
    direct = []
    for quote in quotes_tuple:
        direct.extend((f"{base}/{quote}:{quote}", f"{base}/{quote}"))
    direct.append(base)
    for sym in direct:
        market = ex.markets.get(sym)
        if is_linear_futures(market):
            return sym
    for sym, market in ex.markets.items():
        if str(market.get("base") or "").upper() != base:
            continue
        if str(market.get("quote") or "").upper() not in quotes_tuple:
            continue
        if is_linear_futures(market):
            return sym
    return None


def is_linear_futures(market: Optional[Dict[str, Any]]) -> bool:
    if not market:
        return False
    if not (market.get("swap") or market.get("future") or market.get("contract")):
        return False
    if market.get("linear") is False:
        return False
    return bool(market.get("active", True))


def rows_to_arrays(rows: List[List[float]]) -> Dict[str, np.ndarray]:
    arr = np.asarray(rows, dtype=np.float64)
    if arr.size == 0:
        return {k: np.asarray([], dtype=np.int64 if k == "timestamp_s" else np.float64) for k in OHLCV_COLS}
    return {
        "timestamp_s": (arr[:, 0] // 1000).astype(np.int64),
        "open": arr[:, 1].astype(np.float64),
        "high": arr[:, 2].astype(np.float64),
        "low": arr[:, 3].astype(np.float64),
        "close": arr[:, 4].astype(np.float64),
        "volume": arr[:, 5].astype(np.float64),
    }


def fetch_ohlcv_range(
    ex: Any,
    market: str,
    timeframe: str,
    start_ms: int,
    end_ms: int,
    sleep_sec: float,
    max_empty: int,
    progress_every_requests: int = 0,
) -> Tuple[List[List[float]], Dict[str, Any]]:
    tf_ms = timeframe_ms(ex, timeframe)
    cursor = start_ms
    rows: List[List[float]] = []
    seen_ts = set()
    empty_count = 0
    req_count = 0
    limit_cap = 1440
    last_progress_ms: Optional[int] = None
    while cursor < end_ms:
        remaining = max(0, end_ms - cursor)
        limit = min(limit_cap, max(1, int(math.ceil(remaining / max(tf_ms, 1)))))
        req_count += 1
        batch = ex.fetch_ohlcv(market, timeframe=timeframe, since=cursor, limit=limit)
        if not batch:
            empty_count += 1
            if empty_count >= max_empty:
                break
            cursor += limit * tf_ms
            time.sleep(sleep_sec)
            continue
        empty_count = 0
        accepted = 0
        for row in batch:
            ts = int(row[0])
            if ts < start_ms or ts >= end_ms or ts in seen_ts:
                continue
            seen_ts.add(ts)
            rows.append(row)
            accepted += 1
        last_ts = int(batch[-1][0])
        next_cursor = last_ts + tf_ms
        if next_cursor <= cursor:
            break
        last_progress_ms = last_ts
        cursor = next_cursor
        if accepted == 0 and cursor >= end_ms:
            break
        if progress_every_requests > 0 and req_count % progress_every_requests == 0:
            print(
                f"[progress] {market} requests={req_count} rows={len(rows)} last={ms_to_iso(last_progress_ms)}",
                flush=True,
            )
        time.sleep(sleep_sec)
    rows.sort(key=lambda r: int(r[0]))
    return rows, {
        "requests": req_count,
        "last_progress_dt": ms_to_iso(last_progress_ms) if last_progress_ms else None,
        "stopped_cursor_dt": ms_to_iso(cursor),
    }


def save_symbol_part(path: Path, symbol: str, market: str, arrays: Dict[str, np.ndarray]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "symbols": np.asarray([market], dtype=f"<U{max(len(market), 1)}"),
        "base_symbols": np.asarray([symbol], dtype=f"<U{max(len(symbol), 1)}"),
        "offsets": np.asarray([0, len(arrays["timestamp_s"])], dtype=np.int64),
    }
    payload.update(arrays)
    np.savez_compressed(path, **payload)


def summarize_arrays(arrays: Dict[str, np.ndarray]) -> Dict[str, Any]:
    ts = arrays["timestamp_s"]
    if len(ts) == 0:
        return {"bars": 0, "date_min": None, "date_max": None}
    return {
        "bars": int(len(ts)),
        "date_min": s_to_iso(int(ts[0])),
        "date_max": s_to_iso(int(ts[-1])),
    }


def load_part_arrays(path: Path) -> Tuple[str, Dict[str, np.ndarray]]:
    z = np.load(path)
    symbol = str(z["symbols"][0])
    arrays = {k: z[k] for k in OHLCV_COLS}
    return symbol, arrays


def merge_parts(out_path: Path, part_paths: List[Path]) -> Dict[str, Any]:
    symbols: List[str] = []
    offsets = [0]
    parts: Dict[str, List[np.ndarray]] = {k: [] for k in OHLCV_COLS}
    per_symbol: Dict[str, Dict[str, Any]] = {}
    for part in part_paths:
        symbol, arrays = load_part_arrays(part)
        symbols.append(symbol)
        n = int(len(arrays["timestamp_s"]))
        offsets.append(offsets[-1] + n)
        for col in OHLCV_COLS:
            parts[col].append(arrays[col])
        per_symbol[symbol] = summarize_arrays(arrays)
    max_len = max([len(s) for s in symbols] + [1])
    payload: Dict[str, Any] = {
        "symbols": np.asarray(symbols, dtype=f"<U{max_len}"),
        "offsets": np.asarray(offsets, dtype=np.int64),
    }
    for col in OHLCV_COLS:
        dtype = np.int64 if col == "timestamp_s" else np.float64
        payload[col] = np.concatenate(parts[col]).astype(dtype) if parts[col] else np.asarray([], dtype=dtype)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out_path, **payload)
    return {
        "symbols": symbols,
        "rows": int(offsets[-1]),
        "per_symbol": per_symbol,
    }


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals-csv", default=str(ROOT / "telegram_standard_bt_bundle/telegram_signal_standard_bt/telegram_signals_extracted.csv"))
    ap.add_argument("--universe-file", default=str(ROOT / "universe/telegram_signal_universe_all.txt"))
    ap.add_argument("--exchange", default="bingx")
    ap.add_argument("--timeframe", default="1m")
    ap.add_argument("--quotes", default="USDT")
    ap.add_argument("--out", default=str(ROOT / "DB/telegram_signals_1m_full_window_bingx.npz"))
    ap.add_argument("--parts-dir", default=str(ROOT / "DB/telegram_signals_1m_full_window_bingx_parts"))
    ap.add_argument("--metadata-out", default="")
    ap.add_argument("--sleep-sec", type=float, default=0.12)
    ap.add_argument("--max-empty", type=int, default=3)
    ap.add_argument("--min-bars", type=int, default=1)
    ap.add_argument("--max-symbols", type=int, default=0)
    ap.add_argument("--progress-every-requests", type=int, default=100)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--merge-only", action="store_true")
    args = ap.parse_args()

    signals_csv = Path(args.signals_csv)
    universe_file = Path(args.universe_file)
    out_path = Path(args.out)
    parts_dir = Path(args.parts_dir)
    metadata_path = Path(args.metadata_out) if args.metadata_out else out_path.with_suffix(out_path.suffix + ".meta.json")

    signal_info = inspect_signal_csv(signals_csv)
    requested_symbols = load_universe(universe_file)
    if args.max_symbols > 0:
        requested_symbols = requested_symbols[: args.max_symbols]
    signal_start = floor_minute(signal_info["dt_min"])
    signal_end = ceil_minute(signal_info["dt_max"])
    start_ms = dt_to_ms(signal_start)
    end_ms = dt_to_ms(signal_end)
    quotes = tuple(q.strip().upper() for q in args.quotes.split(",") if q.strip())

    metadata: Dict[str, Any] = {
        "schema": "telegram_signal_1m_window_collection_v1",
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
        "signals_csv": str(signals_csv),
        "universe_file": str(universe_file),
        "requested_signal_min_dt": signal_info["dt_min"].isoformat(),
        "requested_signal_max_dt": signal_info["dt_max"].isoformat(),
        "fetch_start_dt": signal_start.isoformat(),
        "fetch_end_dt_exclusive": signal_end.isoformat(),
        "signal_rows": int(signal_info["rows"]),
        "signal_symbols": signal_info["symbols"],
        "requested_symbols": requested_symbols,
        "exchange": args.exchange,
        "timeframe": args.timeframe,
        "output_npz": str(out_path),
        "parts_dir": str(parts_dir),
        "script": str(Path(__file__)),
        "command": " ".join(sys.argv),
        "per_symbol": {},
        "failures": {},
        "unresolved_symbols": [],
    }

    part_paths: List[Path] = []
    if not args.merge_only:
        ex = build_exchange(args.exchange)
        for idx, raw in enumerate(requested_symbols, start=1):
            part_path = parts_dir / f"{raw}_{args.exchange}_{args.timeframe}.npz"
            if args.resume and part_path.exists():
                symbol, arrays = load_part_arrays(part_path)
                metadata["per_symbol"][raw] = {
                    "market": symbol,
                    "part": str(part_path),
                    **summarize_arrays(arrays),
                    "resumed": True,
                }
                part_paths.append(part_path)
                print(f"[resume] {idx}/{len(requested_symbols)} {raw} part={part_path}", flush=True)
                continue
            market = resolve_linear_futures_market(ex, raw, quotes)
            if not market:
                metadata["unresolved_symbols"].append(raw)
                metadata["failures"][raw] = "unresolved linear futures market"
                write_json(metadata_path, metadata)
                print(f"[skip] {idx}/{len(requested_symbols)} {raw} unresolved", flush=True)
                continue
            try:
                print(f"[fetch] {idx}/{len(requested_symbols)} {raw} market={market} {signal_start.isoformat()}..{signal_end.isoformat()}", flush=True)
                rows, fetch_stats = fetch_ohlcv_range(
                    ex,
                    market,
                    args.timeframe,
                    start_ms,
                    end_ms,
                    sleep_sec=args.sleep_sec,
                    max_empty=args.max_empty,
                    progress_every_requests=args.progress_every_requests,
                )
                arrays = rows_to_arrays(rows)
                stats = summarize_arrays(arrays)
                if stats["bars"] < args.min_bars:
                    metadata["failures"][raw] = f"too few bars: {stats['bars']}"
                    metadata["per_symbol"][raw] = {"market": market, **stats, **fetch_stats}
                    write_json(metadata_path, metadata)
                    print(f"[skip] {idx}/{len(requested_symbols)} {market} too_few_bars={stats['bars']}", flush=True)
                    continue
                save_symbol_part(part_path, raw, market, arrays)
                part_paths.append(part_path)
                metadata["per_symbol"][raw] = {
                    "market": market,
                    "part": str(part_path),
                    **stats,
                    **fetch_stats,
                }
                metadata["updated_at"] = utc_now_iso()
                write_json(metadata_path, metadata)
                print(f"[ok] {idx}/{len(requested_symbols)} {market} bars={stats['bars']} {stats['date_min']}..{stats['date_max']}", flush=True)
            except Exception as exc:
                metadata["failures"][raw] = f"{type(exc).__name__}: {exc}"
                metadata["updated_at"] = utc_now_iso()
                write_json(metadata_path, metadata)
                print(f"[err] {idx}/{len(requested_symbols)} {raw}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)

    if args.merge_only:
        for raw in requested_symbols:
            part_path = parts_dir / f"{raw}_{args.exchange}_{args.timeframe}.npz"
            if part_path.exists():
                part_paths.append(part_path)

    if part_paths:
        merge_info = merge_parts(out_path, part_paths)
        fetched_markets = merge_info["symbols"]
        fetched_bases = sorted(metadata["per_symbol"].keys()) if metadata["per_symbol"] else [
            p.name.split("_", 1)[0] for p in part_paths
        ]
        missing = sorted(set(requested_symbols) - set(fetched_bases))
        metadata.update({
            "updated_at": utc_now_iso(),
            "fetched_symbols": fetched_markets,
            "fetched_base_symbols": fetched_bases,
            "missing_symbols": missing,
            "output_rows": merge_info["rows"],
            "output_symbols_count": len(fetched_markets),
        })
        write_json(metadata_path, metadata)
        print(f"[done] wrote {out_path} symbols={len(fetched_markets)} rows={merge_info['rows']}", flush=True)
        print(f"[done] metadata {metadata_path}", flush=True)
    else:
        metadata.update({
            "updated_at": utc_now_iso(),
            "fetched_symbols": [],
            "fetched_base_symbols": [],
            "missing_symbols": requested_symbols,
            "output_rows": 0,
            "output_symbols_count": 0,
        })
        write_json(metadata_path, metadata)
        raise SystemExit("no symbol parts fetched")


if __name__ == "__main__":
    main()
