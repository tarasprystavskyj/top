#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bybit_ticks_to_1s_json.py

Downloads daily Bybit public trade tick files for the last N days,
extracts them, aggregates trades into 1-second bars, and writes JSON/JSONL.

Default output record format:
  {"price": 0.1034, "timestamp": 1773073750000, "volume": 591.79}

Optional OHLCV format:
  {"timestamp": 1773073750000, "open": ..., "high": ..., "low": ..., "close": ..., "volume": ...}

Examples:

  # Last 7 days for ENAUSDT -> 1s JSONL close+volume format
  python3 bybit_ticks_to_1s_json.py \
    --symbol ENAUSDT \
    --days 7 \
    --output DB/ENAUSDT_1s_last7d.jsonl

  # Same, but keep downloaded .csv.gz and extracted .csv files
  python3 bybit_ticks_to_1s_json.py \
    --symbol ENAUSDT \
    --days 7 \
    --output DB/ENAUSDT_1s_last7d.jsonl \
    --keep-files

  # Write OHLCV JSONL
  python3 bybit_ticks_to_1s_json.py \
    --symbol ENAUSDT \
    --days 30 \
    --output DB/ENAUSDT_1s_ohlcv_last30d.jsonl \
    --format ohlcv

  # Write one JSON array instead of JSONL
  python3 bybit_ticks_to_1s_json.py \
    --symbol ENAUSDT \
    --days 3 \
    --output DB/ENAUSDT_1s_last3d.json \
    --format close

Notes:
- Uses Bybit public archive layout:
    https://public.bybit.com/trading/<SYMBOL>/<SYMBOL>YYYY-MM-DD.csv.gz
- By default processes days from today backwards, inclusive of today.
- Missing days are skipped with a warning.
"""

import argparse
import csv
import gzip
import json
import os
import shutil
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


BASE_URL = "https://public.bybit.com/trading"


@dataclass
class TickRow:
    timestamp_ms: int
    price: float
    volume: float


def log(msg: str) -> None:
    print(msg, flush=True)


def daterange_last_n_days(days: int) -> List[str]:
    today = datetime.now(timezone.utc).date()
    return [(today - timedelta(days=i)).isoformat() for i in range(days)]


def download_file(url: str, out_path: Path, timeout: int = 60) -> bool:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urlopen(url, timeout=timeout) as resp, open(out_path, "wb") as f:
            shutil.copyfileobj(resp, f)
        return True
    except HTTPError as e:
        if e.code == 404:
            return False
        raise
    except URLError:
        raise


def decompress_gzip(gz_path: Path, csv_path: Path) -> None:
    with gzip.open(gz_path, "rb") as src, open(csv_path, "wb") as dst:
        shutil.copyfileobj(src, dst)


def detect_columns(header: List[str]) -> Dict[str, str]:
    lowered = {h.strip().lower(): h for h in header}

    def pick(candidates: List[str], label: str) -> str:
        for c in candidates:
            if c.lower() in lowered:
                return lowered[c.lower()]
        raise ValueError(f"Cannot detect {label} column in header: {header}")

    return {
        "timestamp": pick(["timestamp", "ts", "time", "trade_time", "t", "exec_time"], "timestamp"),
        "price": pick(["price", "p", "last_price", "exec_price"], "price"),
        "volume": pick(["size", "qty", "quantity", "volume", "q", "exec_qty"], "volume"),
    }


def normalize_timestamp_to_ms(raw: str) -> int:
    s = raw.strip()

    # Numeric epoch
    try:
        v = int(float(s))
        if v > 10**17:   # ns
            return v // 10**6
        if v > 10**14:   # us
            return v // 10**3
        if v > 10**11:   # ms
            return v
        return v * 1000  # seconds
    except Exception:
        pass

    # ISO datetime
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return int(dt.timestamp() * 1000)


def iter_ticks_from_csv(csv_path: Path) -> Iterable[TickRow]:
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {csv_path}")
        cols = detect_columns(reader.fieldnames)

        for row in reader:
            try:
                ts = normalize_timestamp_to_ms(str(row[cols["timestamp"]]))
                price = float(row[cols["price"]])
                volume = float(row[cols["volume"]])
                yield TickRow(timestamp_ms=ts, price=price, volume=volume)
            except Exception:
                continue


def aggregate_ticks_to_1s_rows(
    ticks: Iterable[TickRow],
    output_format: str = "close",
) -> List[dict]:
    """
    output_format:
      - close: {"price", "timestamp", "volume"}
      - ohlcv: {"timestamp", "open", "high", "low", "close", "volume"}
    """
    buckets: Dict[int, dict] = {}

    for t in ticks:
        sec_ms = (t.timestamp_ms // 1000) * 1000
        b = buckets.get(sec_ms)
        if b is None:
            buckets[sec_ms] = {
                "timestamp": sec_ms,
                "open": t.price,
                "high": t.price,
                "low": t.price,
                "close": t.price,
                "volume": t.volume,
            }
        else:
            if t.price > b["high"]:
                b["high"] = t.price
            if t.price < b["low"]:
                b["low"] = t.price
            b["close"] = t.price
            b["volume"] += t.volume

    out: List[dict] = []
    for sec_ms in sorted(buckets.keys()):
        b = buckets[sec_ms]
        if output_format == "ohlcv":
            out.append({
                "timestamp": int(b["timestamp"]),
                "open": float(b["open"]),
                "high": float(b["high"]),
                "low": float(b["low"]),
                "close": float(b["close"]),
                "volume": float(b["volume"]),
            })
        else:
            out.append({
                "price": float(b["close"]),
                "timestamp": int(b["timestamp"]),
                "volume": float(b["volume"]),
            })
    return out


def write_jsonl(rows: List[dict], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for row in rows:
            json.dump(row, f, ensure_ascii=False)
            f.write("\n")


def write_json(rows: List[dict], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)


def append_jsonl(rows: List[dict], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "a", encoding="utf-8") as f:
        for row in rows:
            json.dump(row, f, ensure_ascii=False)
            f.write("\n")


def load_existing_json(path: Path) -> List[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def sort_and_dedup_rows(rows: List[dict], output_format: str) -> List[dict]:
    merged: Dict[int, dict] = {}
    for r in rows:
        ts = int(r["timestamp"])
        merged[ts] = r
    return [merged[k] for k in sorted(merged.keys())]


def process_one_day(
    symbol: str,
    day_str: str,
    workdir: Path,
    keep_files: bool,
    output_format: str,
) -> List[dict]:
    filename = f"{symbol}{day_str}.csv.gz"
    url = f"{BASE_URL}/{symbol}/{filename}"

    gz_path = workdir / symbol / filename
    csv_path = gz_path.with_suffix("")  # removes .gz

    log(f"[GET] {url}")
    ok = download_file(url, gz_path)
    if not ok:
        log(f"[MISS] {day_str} not found for {symbol}")
        return []

    log(f"[UNZIP] {gz_path.name}")
    decompress_gzip(gz_path, csv_path)

    log(f"[READ] {csv_path.name}")
    ticks = iter_ticks_from_csv(csv_path)
    rows = aggregate_ticks_to_1s_rows(ticks, output_format=output_format)
    log(f"[OK] {day_str} seconds={len(rows)}")

    if not keep_files:
        try:
            csv_path.unlink(missing_ok=True)
            gz_path.unlink(missing_ok=True)
        except Exception:
            pass

    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True, help="Bybit symbol, e.g. ENAUSDT")
    ap.add_argument("--days", type=int, required=True, help="How many days back from today to process")
    ap.add_argument("--output", required=True, help="Output .json or .jsonl")
    ap.add_argument("--format", choices=["close", "ohlcv"], default="close",
                    help="close => {price,timestamp,volume}; ohlcv => 1s OHLCV")
    ap.add_argument("--workdir", default="bybit_ticks_work", help="Temp directory for downloaded files")
    ap.add_argument("--keep-files", action="store_true", help="Keep downloaded .csv.gz and extracted .csv")
    ap.add_argument("--append", action="store_true",
                    help="Append to existing output. For .json, loads existing file, merges, sorts, dedups by timestamp.")
    args = ap.parse_args()

    if args.days <= 0:
        raise SystemExit("--days must be > 0")

    symbol = args.symbol.upper().strip()
    workdir = Path(args.workdir)
    out_path = Path(args.output)
    output_is_jsonl = out_path.suffix.lower() in {".jsonl", ".ndjson"}

    day_list = daterange_last_n_days(args.days)

    all_rows: List[dict] = []
    for day_str in reversed(day_list):
        try:
            rows = process_one_day(
                symbol=symbol,
                day_str=day_str,
                workdir=workdir,
                keep_files=args.keep_files,
                output_format=args.format,
            )

            if output_is_jsonl and args.append:
                append_jsonl(rows, out_path)
            else:
                all_rows.extend(rows)

        except Exception as e:
            log(f"[ERR] {day_str} {e}")

    if not (output_is_jsonl and args.append):
        if args.append and not output_is_jsonl:
            existing = load_existing_json(out_path)
            all_rows = existing + all_rows

        all_rows = sort_and_dedup_rows(all_rows, output_format=args.format)

        if output_is_jsonl:
            write_jsonl(all_rows, out_path)
        else:
            write_json(all_rows, out_path)

    log(f"[DONE] symbol={symbol} days={args.days} output={out_path}")


if __name__ == "__main__":
    main()
