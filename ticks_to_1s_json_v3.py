#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ticks_to_1s_json_v3.py

Download public daily trade/tick archives from supported exchanges and convert them
to 1-second bars in JSON/JSONL.

Supported exchanges:
- bybit
- okx
- bingx

Features:
- --exchange {bybit,okx,bingx}
- --symbol SYMBOL
- either --days N (default ends on yesterday UTC) OR --start-date/--end-date
- --include-today
- --fill-missing-seconds
- --split-by {none,month,week}
- one dedicated session subfolder per export
- filenames include symbol, exchange, start date, end date, and chunk label
- output formats:
    close: {"price": ..., "timestamp": ..., "volume": ...}
    ohlcv: {"timestamp": ..., "open": ..., "high": ..., "low": ..., "close": ..., "volume": ...}
- output extension:
    .jsonl / .json
- optional concurrent downloads via --workers
- optional retries via --retries
- optional keep downloaded files via --keep-files

Examples:

  python3 ticks_to_1s_json_v3.py \
    --exchange bybit \
    --symbol ENAUSDT \
    --start-date 2024-04-01 \
    --end-date 2024-06-30 \
    --split-by month \
    --fill-missing-seconds \
    --output-dir DB

  python3 ticks_to_1s_json_v3.py \
    --exchange okx \
    --symbol ENAUSDT \
    --days 30 \
    --output-dir DB \
    --split-by week

  python3 ticks_to_1s_json_v3.py \
    --exchange bingx \
    --symbol BTCUSDT \
    --days 7 \
    --output-dir DB \
    --ext json
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


SUPPORTED_EXCHANGES = {"bybit", "okx", "bingx"}


@dataclass
class TickRow:
    timestamp_ms: int
    price: float
    volume: float


def log(msg: str) -> None:
    print(msg, flush=True)


def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def daterange(start_date: date, end_date: date) -> List[str]:
    if end_date < start_date:
        raise ValueError("end_date must be >= start_date")
    days = (end_date - start_date).days + 1
    return [(start_date + timedelta(days=i)).isoformat() for i in range(days)]


def last_n_days_range(days: int, end_yesterday: bool = True) -> Tuple[date, date]:
    if days <= 0:
        raise ValueError("days must be > 0")
    anchor = datetime.now(timezone.utc).date()
    if end_yesterday:
        anchor = anchor - timedelta(days=1)
    start = anchor - timedelta(days=days - 1)
    return start, anchor


def normalize_symbol_for_url(exchange: str, symbol: str) -> str:
    s = symbol.upper().strip()
    if exchange == "bybit":
        return s
    if exchange in {"okx", "bingx"}:
        # Convert BTCUSDT -> BTC-USDT for common USDT pairs, but preserve existing separators.
        if "-" in s:
            return s
        if "/" in s:
            return s.replace("/", "-").replace(":USDT", "").replace(":USDC", "")
        if s.endswith("USDT") and len(s) > 4:
            return f"{s[:-4]}-USDT"
        if s.endswith("USDC") and len(s) > 4:
            return f"{s[:-4]}-USDC"
        return s
    raise ValueError(f"Unsupported exchange: {exchange}")


def build_download_url(exchange: str, symbol: str, day_str: str) -> str:
    ex = exchange.lower().strip()
    sym = normalize_symbol_for_url(ex, symbol)

    if ex == "bybit":
        # https://public.bybit.com/trading/ENAUSDT/ENAUSDT2024-04-02.csv.gz
        return f"https://public.bybit.com/trading/{sym}/{sym}{day_str}.csv.gz"

    if ex == "okx":
        # Common OKX public archive pattern
        # https://www.okx.com/cdn/okex/traderecords/trades/daily/ENA-USDT/ENA-USDT-trades-2024-04-01.csv.gz
        return f"https://www.okx.com/cdn/okex/traderecords/trades/daily/{sym}/{sym}-trades-{day_str}.csv.gz"

    if ex == "bingx":
        # Public S3 archive pattern
        # https://bingx-public-data.s3.amazonaws.com/trades/ENA-USDT/2024-04-01.csv.gz
        return f"https://bingx-public-data.s3.amazonaws.com/trades/{sym}/{day_str}.csv.gz"

    raise ValueError(f"Unsupported exchange: {exchange}")


def session_dir_name(exchange: str, symbol: str, start_date: date, end_date: date) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{symbol.upper()}-{exchange.lower()}-{start_date.isoformat()}-{end_date.isoformat()}-{stamp}"


def chunk_month_label(d: date) -> str:
    return d.strftime("%Y-%m")


def week_bounds(d: date) -> Tuple[date, date]:
    monday = d - timedelta(days=d.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def chunk_week_label(d: date) -> str:
    ws, we = week_bounds(d)
    return f"{ws.isoformat()}_{we.isoformat()}"


def build_output_filename(symbol: str, exchange: str, start_date: date, end_date: date, ext: str, chunk_label: Optional[str] = None) -> str:
    base = f"{symbol.upper()}-{exchange.lower()}-{start_date.isoformat()}-{end_date.isoformat()}"
    if chunk_label:
        base += f"-{chunk_label}"
    return f"{base}.{ext}"


def group_days(day_strings: List[str], split_by: str) -> List[Tuple[str, List[str]]]:
    if split_by == "none":
        return [("all", day_strings)]

    grouped: Dict[str, List[str]] = {}
    for ds in day_strings:
        d = parse_date(ds)
        if split_by == "month":
            label = chunk_month_label(d)
        elif split_by == "week":
            label = chunk_week_label(d)
        else:
            raise ValueError(f"Unsupported split_by: {split_by}")
        grouped.setdefault(label, []).append(ds)

    return sorted(grouped.items(), key=lambda x: x[1][0])


def download_file(url: str, out_path: Path, timeout: int = 60, retries: int = 2, sleep_sec: float = 1.5) -> bool:
    out_path.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(retries + 1):
        try:
            with urlopen(url, timeout=timeout) as resp, open(out_path, "wb") as f:
                shutil.copyfileobj(resp, f)
            return True
        except HTTPError as e:
            if e.code == 404:
                return False
            if attempt >= retries:
                raise
            time.sleep(sleep_sec)
        except URLError:
            if attempt >= retries:
                raise
            time.sleep(sleep_sec)

    return False


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
        "timestamp": pick(
            ["timestamp", "ts", "time", "trade_time", "t", "exec_time", "transacttime"],
            "timestamp",
        ),
        "price": pick(
            ["price", "px", "p", "last_price", "exec_price"],
            "price",
        ),
        "volume": pick(
            ["size", "qty", "quantity", "volume", "q", "exec_qty", "sz"],
            "volume",
        ),
    }


def normalize_timestamp_to_ms(raw: str) -> int:
    s = raw.strip()
    try:
        v = int(float(s))
        if v > 10**17:   # ns
            return v // 10**6
        if v > 10**14:   # us
            return v // 10**3
        if v > 10**11:   # ms
            return v
        return v * 1000  # sec
    except Exception:
        pass

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


def aggregate_ticks_to_1s_rows(ticks: Iterable[TickRow], output_format: str = "close") -> List[dict]:
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


def sort_and_dedup_rows(rows: List[dict]) -> List[dict]:
    merged: Dict[int, dict] = {}
    for r in rows:
        merged[int(r["timestamp"])] = r
    return [merged[k] for k in sorted(merged.keys())]


def fill_missing_seconds(rows: List[dict], start_date: date, end_date: date, output_format: str) -> List[dict]:
    if start_date > end_date or not rows:
        return rows

    start_ms = int(datetime(start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc).timestamp() * 1000)
    end_exclusive_ms = int((datetime(end_date.year, end_date.month, end_date.day, tzinfo=timezone.utc) + timedelta(days=1)).timestamp() * 1000)

    row_map = {int(r["timestamp"]): r for r in rows}
    sorted_ts = sorted(row_map.keys())
    first_ts = sorted_ts[0]
    last_ts = sorted_ts[-1]

    # Fill within actual observed bounds clipped by requested calendar interval.
    start_ms = max(start_ms, first_ts)
    end_exclusive_ms = min(end_exclusive_ms, last_ts + 1000)

    filled: List[dict] = []
    prev = row_map.get(first_ts)

    for ts in range(start_ms, end_exclusive_ms, 1000):
        current = row_map.get(ts)
        if current is not None:
            filled.append(current)
            prev = current
        elif prev is not None:
            if output_format == "ohlcv":
                px = float(prev["close"])
                filled.append({
                    "timestamp": ts,
                    "open": px,
                    "high": px,
                    "low": px,
                    "close": px,
                    "volume": 0.0,
                })
            else:
                filled.append({
                    "price": float(prev["price"]),
                    "timestamp": ts,
                    "volume": 0.0,
                })

    return filled


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


def download_and_process_one_day(
    exchange: str,
    symbol: str,
    day_str: str,
    workdir: Path,
    keep_files: bool,
    output_format: str,
    timeout: int,
    retries: int,
) -> Tuple[str, List[dict], Optional[str]]:
    """
    Returns: (day_str, rows, error_message_or_none)
    """
    url = build_download_url(exchange, symbol, day_str)
    safe_exchange = exchange.lower()
    safe_symbol = normalize_symbol_for_url(exchange, symbol).replace("/", "-").replace(":", "-")

    filename = Path(url).name
    gz_path = workdir / safe_exchange / safe_symbol / filename
    csv_path = gz_path.with_suffix("")

    try:
        log(f"[GET] {url}")
        ok = download_file(url, gz_path, timeout=timeout, retries=retries)
        if not ok:
            log(f"[MISS] {day_str} not found for {symbol} on {exchange}")
            return day_str, [], None

        log(f"[UNZIP] {gz_path.name}")
        decompress_gzip(gz_path, csv_path)

        log(f"[READ] {csv_path.name}")
        ticks = iter_ticks_from_csv(csv_path)
        rows = aggregate_ticks_to_1s_rows(ticks, output_format=output_format)
        log(f"[OK] {exchange} {day_str} seconds={len(rows)}")

        if not keep_files:
            try:
                csv_path.unlink(missing_ok=True)
                gz_path.unlink(missing_ok=True)
            except Exception:
                pass

        return day_str, rows, None

    except Exception as e:
        return day_str, [], str(e)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exchange", choices=["bybit", "okx", "bingx"], required=True, help="Exchange source")
    ap.add_argument("--symbol", required=True, help="Symbol, e.g. ENAUSDT")
    ap.add_argument("--days", type=int, default=None, help="How many days back to process")
    ap.add_argument("--include-today", action="store_true", help="Include current UTC date as newest day. Default ends yesterday.")
    ap.add_argument("--start-date", default=None, help="Start date YYYY-MM-DD")
    ap.add_argument("--end-date", default=None, help="End date YYYY-MM-DD")
    ap.add_argument("--output-dir", required=True, help="Base output directory; a per-session subfolder will be created")
    ap.add_argument("--format", choices=["close", "ohlcv"], default="close")
    ap.add_argument("--ext", choices=["jsonl", "json"], default="jsonl")
    ap.add_argument("--split-by", choices=["none", "month", "week"], default="none")
    ap.add_argument("--fill-missing-seconds", action="store_true", help="Fill empty seconds with previous price and zero volume")
    ap.add_argument("--workdir", default="ticks_work", help="Temp directory for downloaded archives")
    ap.add_argument("--keep-files", action="store_true", help="Keep downloaded .csv.gz and extracted .csv")
    ap.add_argument("--workers", type=int, default=1, help="Parallel day downloads per chunk")
    ap.add_argument("--timeout", type=int, default=60, help="Download timeout per request in seconds")
    ap.add_argument("--retries", type=int, default=2, help="Retry count for non-404 download failures")
    args = ap.parse_args()

    exchange = args.exchange.lower().strip()
    symbol = args.symbol.upper().strip()

    if exchange not in SUPPORTED_EXCHANGES:
        raise SystemExit(f"Unsupported exchange: {exchange}")

    if args.start_date or args.end_date:
        if not (args.start_date and args.end_date):
            raise SystemExit("Both --start-date and --end-date must be provided together")
        start_date = parse_date(args.start_date)
        end_date = parse_date(args.end_date)
    else:
        if args.days is None:
            raise SystemExit("Provide either --days or both --start-date and --end-date")
        start_date, end_date = last_n_days_range(args.days, end_yesterday=not args.include_today)

    if end_date < start_date:
        raise SystemExit("--end-date must be >= --start-date")
    if args.workers < 1:
        raise SystemExit("--workers must be >= 1")

    day_list = daterange(start_date, end_date)
    base_output_dir = Path(args.output_dir)
    session_dir = base_output_dir / session_dir_name(exchange, symbol, start_date, end_date)
    session_dir.mkdir(parents=True, exist_ok=True)
    log(f"[SESSION] {session_dir}")

    grouped = group_days(day_list, args.split_by)
    workdir = Path(args.workdir)

    manifest = {
        "exchange": exchange,
        "symbol": symbol,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "split_by": args.split_by,
        "format": args.format,
        "ext": args.ext,
        "fill_missing_seconds": bool(args.fill_missing_seconds),
        "workers": args.workers,
        "timeout": args.timeout,
        "retries": args.retries,
        "session_dir": str(session_dir),
        "files": [],
        "missed_days": [],
        "errors": [],
    }

    for chunk_label, chunk_days in grouped:
        log(f"[CHUNK] {chunk_label} days={len(chunk_days)}")
        day_rows: Dict[str, List[dict]] = {}

        if args.workers == 1:
            for day_str in chunk_days:
                ds, rows, err = download_and_process_one_day(
                    exchange=exchange,
                    symbol=symbol,
                    day_str=day_str,
                    workdir=workdir,
                    keep_files=args.keep_files,
                    output_format=args.format,
                    timeout=args.timeout,
                    retries=args.retries,
                )
                day_rows[ds] = rows
                if err:
                    manifest["errors"].append({"day": ds, "error": err})
                    log(f"[ERR] {exchange} {ds} {err}")
                elif not rows:
                    manifest["missed_days"].append(ds)
        else:
            futures = []
            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                for day_str in chunk_days:
                    futures.append(
                        ex.submit(
                            download_and_process_one_day,
                            exchange,
                            symbol,
                            day_str,
                            workdir,
                            args.keep_files,
                            args.format,
                            args.timeout,
                            args.retries,
                        )
                    )
                for fut in as_completed(futures):
                    ds, rows, err = fut.result()
                    day_rows[ds] = rows
                    if err:
                        manifest["errors"].append({"day": ds, "error": err})
                        log(f"[ERR] {exchange} {ds} {err}")
                    elif not rows:
                        manifest["missed_days"].append(ds)

        all_rows: List[dict] = []
        for ds in chunk_days:
            all_rows.extend(day_rows.get(ds, []))

        all_rows = sort_and_dedup_rows(all_rows)

        if args.fill_missing_seconds and all_rows:
            cd_start = parse_date(chunk_days[0])
            cd_end = parse_date(chunk_days[-1])
            all_rows = fill_missing_seconds(all_rows, cd_start, cd_end, args.format)

        if chunk_label == "all":
            file_start = start_date
            file_end = end_date
            out_name = build_output_filename(symbol, exchange, file_start, file_end, args.ext)
        else:
            file_start = parse_date(chunk_days[0])
            file_end = parse_date(chunk_days[-1])
            out_name = build_output_filename(symbol, exchange, file_start, file_end, args.ext, chunk_label=chunk_label)

        out_path = session_dir / out_name
        if args.ext == "jsonl":
            write_jsonl(all_rows, out_path)
        else:
            write_json(all_rows, out_path)

        manifest["files"].append({
            "chunk": chunk_label,
            "path": str(out_path),
            "rows": len(all_rows),
            "start_date": file_start.isoformat(),
            "end_date": file_end.isoformat(),
        })
        log(f"[WRITE] {out_path.name} rows={len(all_rows)}")

    manifest_path = session_dir / "manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    log(f"[DONE] session={session_dir}")


if __name__ == "__main__":
    main()
