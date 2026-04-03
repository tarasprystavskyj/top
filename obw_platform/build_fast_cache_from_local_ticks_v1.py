#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build standard SQLite cache + optional fast NPZ cache from local monthly tick JSONL files.

This wrapper intentionally reuses:
  - fetch_build_cache_from_ticks_v1.py  -> schema, timeframe helpers, bar features
  - build_dual_fast_cache.py            -> compact NPZ fast cache builder

Expected input folder example:
  DB/ENAUSDT-bybit-2025-03-01-2026-03-01-20260316_190351/

Expected file naming example:
  ENAUSDT-bybit-2025-03-01-2025-03-31-2025-03.jsonl

Each line in JSONL:
  {"price": 0.4139, "timestamp": 1740787200000, "volume": 10638.0}

New filtering modes:
  - by exact datetime interval: --start / --end
  - by months: --months 2025-03,2025-04
  - by month range: --month-from 2025-03 --month-to 2025-08
  - separate output per month: --per-month
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import pandas as pd

FILE_RE = re.compile(
    r"^(?P<pair>[A-Z0-9]+)-(?P<exchange>[a-z0-9_]+)-(?P<start>\d{4}-\d{2}-\d{2})-(?P<end>\d{4}-\d{2}-\d{2})-(?P<month>\d{4}-\d{2})\.jsonl(?:\.txt)?$"
)


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise SystemExit(f"Cannot load module from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def month_to_int(month_token: str) -> int:
    if not re.fullmatch(r"\d{4}-\d{2}", month_token):
        raise SystemExit(f"Bad month token: {month_token!r}. Expected YYYY-MM")
    y, m = month_token.split("-")
    return int(y) * 12 + int(m)


def parse_datetime_to_ms(s: str) -> int:
    ts = pd.to_datetime(s, utc=True)
    if pd.isna(ts):
        raise SystemExit(f"Could not parse datetime: {s!r}")
    return int(ts.value // 10**6)


def fmt_ms(ms: Optional[int]) -> str:
    if ms is None:
        return "-"
    return pd.to_datetime(ms, unit="ms", utc=True).strftime("%Y-%m-%d %H:%M:%S UTC")


def discover_files(ticks_dir: Path) -> List[Path]:
    files: List[Path] = []
    for p in ticks_dir.iterdir():
        if p.is_file() and (p.name.endswith(".jsonl") or p.name.endswith(".jsonl.txt")):
            files.append(p)

    if not files:
        raise SystemExit(f"No .jsonl files found in {ticks_dir}")

    def sort_key(p: Path):
        m = FILE_RE.match(p.name)
        if m:
            return (m.group("start"), m.group("end"), p.name)
        return ("9999-99-99", "9999-99-99", p.name)

    return sorted(files, key=sort_key)


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
        pair = m.group("pair").upper()
        for quote in ("USDT", "USDC", "BTC", "ETH"):
            if pair.endswith(quote) and len(pair) > len(quote):
                base = pair[:-len(quote)]
                if symbol_format == "usdtm":
                    return f"{base}/{quote}:{quote}"
                if symbol_format == "usdt":
                    return f"{base}/{quote}"
                if symbol_format == "raw":
                    return pair
                raise SystemExit(f"Unsupported symbol format: {symbol_format}")
    raise SystemExit(
        "Could not derive market symbol from folder/files. Pass --market-symbol explicitly."
    )


def parse_file_meta(path: Path) -> Optional[dict]:
    m = FILE_RE.match(path.name)
    if not m:
        return None
    start_ms = parse_datetime_to_ms(m.group("start") + " 00:00:00")
    end_ms_excl = parse_datetime_to_ms(m.group("end") + " 00:00:00") + 24 * 3600 * 1000
    return {
        "path": path,
        "pair": m.group("pair"),
        "exchange": m.group("exchange"),
        "start_ms": start_ms,
        "end_ms_excl": end_ms_excl,
        "month": m.group("month"),
    }


def filter_files(
    files: Sequence[Path],
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
            month_token = meta["month"]
            month_i = month_to_int(month_token)
            if exact_months and month_token not in exact_months:
                continue
            if month_from_i is not None and month_i < month_from_i:
                continue
            if month_to_i is not None and month_i > month_to_i:
                continue
            if start_ms is not None and meta["end_ms_excl"] <= start_ms:
                continue
            if end_ms_excl is not None and meta["start_ms"] >= end_ms_excl:
                continue
        out.append(p)

    if not out:
        raise SystemExit("No input files remain after month/date filtering")
    return out


def iter_tick_rows(
    files: Iterable[Path],
    start_ms: Optional[int],
    end_ms_excl: Optional[int],
) -> Iterator[Tuple[int, float, float]]:
    for path in files:
        print(f"[READ] {path}")
        with path.open("r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, start=1):
                s = line.strip()
                if not s:
                    continue
                try:
                    obj = json.loads(s)
                except json.JSONDecodeError as exc:
                    raise SystemExit(f"JSON parse error in {path}:{line_no}: {exc}") from exc

                ts = obj.get("timestamp")
                price = obj.get("price")
                volume = obj.get("volume")
                if ts is None or price is None or volume is None:
                    continue

                try:
                    ts_i = int(ts)
                    price_f = float(price)
                    volume_f = float(volume)
                except Exception as exc:
                    raise SystemExit(f"Bad numeric value in {path}:{line_no}: {obj}") from exc

                if start_ms is not None and ts_i < start_ms:
                    continue
                if end_ms_excl is not None and ts_i >= end_ms_excl:
                    continue

                yield ts_i, price_f, volume_f


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
        dt = pd.to_datetime(bucket_ms, unit="ms", utc=True).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        rows.append((dt, open_, high_, low_, close_, vol_))
        count_bars += 1

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
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    df = pd.DataFrame(rows, columns=["datetime_utc", "open", "high", "low", "close", "volume"])
    df = df.set_index("datetime_utc")
    print(f"[AGG] ticks={count_ticks} bars={count_bars}")
    return df


def build_rows_from_features(feats: pd.DataFrame, market_symbol: str) -> List[dict]:
    out: List[dict] = []
    for idx, r in feats.iterrows():
        out.append({
            "symbol": market_symbol,
            "datetime_utc": idx,
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "volume": float(r["volume"]),
            "rsi": float(r.get("rsi", 0.0)),
            "stochastic": float(r.get("stochastic", 0.0)),
            "mfi": float(r.get("mfi", 0.0)),
            "overbought_index": float(r.get("overbought_index", 0.0)),
            "atr_ratio": float(r["atr_ratio"]),
            "gain_24h_before": float(r["gain_24h_before"]),
            "dp6h": float(r["dp6h"]),
            "dp12h": float(r["dp12h"]),
            "quote_volume": float(r["quote_volume"]),
            "qv_24h": float(r["qv_24h"]),
            "vol_surge_mult": float(r["vol_surge_mult"]),
        })
    return out


def path_with_month(base_path: Path, month_token: str) -> Path:
    return base_path.with_name(f"{base_path.stem}_{month_token}{base_path.suffix}")


def ensure_fresh_db(fetch_mod, db_path: Path, fresh: bool) -> None:
    fetch_mod.ensure_schema(str(db_path))
    if fresh:
        import sqlite3
        con = sqlite3.connect(str(db_path))
        cur = con.cursor()
        cur.execute("DROP TABLE IF EXISTS price_indicators")
        con.commit()
        con.close()
        fetch_mod.ensure_schema(str(db_path))


def process_batch(
    *,
    files: Sequence[Path],
    start_ms: Optional[int],
    end_ms_excl: Optional[int],
    timeframe: str,
    tf_seconds: int,
    tf_ms: int,
    market_symbol: str,
    fetch_mod,
    db_path: Path,
    fast_out: Optional[Path],
    fast_dep: Optional[Path],
    fresh: bool,
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    ensure_fresh_db(fetch_mod, db_path, fresh=fresh)

    print(f"[CFG] files={len(files)} timeframe={timeframe} market_symbol={market_symbol}")
    print(f"[CFG] start={fmt_ms(start_ms)} end_exclusive={fmt_ms(end_ms_excl)}")
    print(f"[CFG] db={db_path}")

    bars_df = aggregate_ticks_to_bars_stream(
        iter_tick_rows(files, start_ms=start_ms, end_ms_excl=end_ms_excl),
        tf_ms=tf_ms,
    )
    if bars_df.empty:
        print("[SKIP] no bars built after filtering")
        return

    print(f"[FEATURES] computing on {len(bars_df)} bars ...")
    feats = fetch_mod.compute_features(bars_df, tf_seconds=tf_seconds)
    rows = build_rows_from_features(feats, market_symbol=market_symbol)
    fetch_mod.insert_ignore_rows(str(db_path), rows)
    print(f"[DB] inserted rows={len(rows)} into {db_path}")

    if fast_out and fast_dep:
        fast_out.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            sys.executable,
            str(fast_dep),
            "--db", str(db_path),
            "--symbol", market_symbol,
            "--out", str(fast_out),
        ]
        print("[FAST] " + " ".join(cmd))
        subprocess.run(cmd, check=True)
        print(f"[DONE] db={db_path} fast={fast_out}")
    else:
        print(f"[DONE] db={db_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Build SQLite + optional fast NPZ cache from local monthly tick JSONL files")
    ap.add_argument("--ticks-dir", required=True, help="Folder with monthly tick JSONL files")
    ap.add_argument("--timeframe", "-t", required=True, help="Target timeframe, e.g. 1s, 30s, 1m, 5m")
    ap.add_argument("--db", required=True, help="Output SQLite DB path; with --per-month becomes filename template")
    ap.add_argument("--fast-out", default="", help="Optional output NPZ path; with --per-month becomes filename template")
    ap.add_argument("--market-symbol", default="", help="Exact market symbol to store in DB, e.g. ENA/USDT:USDT")
    ap.add_argument("--symbol-format", choices=["usdtm", "usdt", "raw"], default="usdtm", help="How to derive symbol from filename when --market-symbol is omitted")
    ap.add_argument("--start", default="", help="UTC start datetime inclusive, e.g. '2025-03-10 12:00:00'")
    ap.add_argument("--end", default="", help="UTC end datetime exclusive, e.g. '2025-03-20 00:00:00'")
    ap.add_argument("--months", default="", help="Comma-separated exact months: 2025-03,2025-04")
    ap.add_argument("--month-from", default="", help="Start month inclusive: YYYY-MM")
    ap.add_argument("--month-to", default="", help="End month inclusive: YYYY-MM")
    ap.add_argument("--per-month", action="store_true", help="Build separate DB/NPZ for each month")
    ap.add_argument("--fetch-dep", default="", help="Path to fetch_build_cache_from_ticks_v1.py (default: same dir as this script)")
    ap.add_argument("--fast-dep", default="", help="Path to build_dual_fast_cache.py (default: same dir as this script)")
    ap.add_argument("--fresh", action="store_true", help="Drop and recreate price_indicators table")
    args = ap.parse_args()

    self_dir = Path(__file__).resolve().parent
    fetch_dep = Path(args.fetch_dep).resolve() if args.fetch_dep else (self_dir / "fetch_build_cache_from_ticks_v1.py")
    fast_dep = Path(args.fast_dep).resolve() if args.fast_out and args.fast_dep else ((self_dir / "build_dual_fast_cache.py") if args.fast_out else None)

    if not fetch_dep.exists():
        raise SystemExit(f"Dependency not found: {fetch_dep}")
    if fast_dep is not None and not fast_dep.exists():
        raise SystemExit(f"Dependency not found: {fast_dep}")

    fetch_mod = load_module(fetch_dep, "fetch_build_cache_from_ticks_v1_dep")

    ticks_dir = Path(args.ticks_dir).resolve()
    if not ticks_dir.exists() or not ticks_dir.is_dir():
        raise SystemExit(f"ticks-dir is not a directory: {ticks_dir}")

    all_files = discover_files(ticks_dir)

    exact_months = None
    if args.months.strip():
        exact_months = {m.strip() for m in args.months.split(",") if m.strip()}
        for m in exact_months:
            month_to_int(m)

    start_ms = parse_datetime_to_ms(args.start) if args.start.strip() else None
    end_ms_excl = parse_datetime_to_ms(args.end) if args.end.strip() else None
    if start_ms is not None and end_ms_excl is not None and start_ms >= end_ms_excl:
        raise SystemExit("--start must be earlier than --end")

    files = filter_files(
        all_files,
        exact_months=exact_months,
        month_from=args.month_from.strip() or None,
        month_to=args.month_to.strip() or None,
        start_ms=start_ms,
        end_ms_excl=end_ms_excl,
    )

    market_symbol = derive_market_symbol(
        ticks_dir=ticks_dir,
        files=files,
        symbol_format=args.symbol_format,
        explicit_market_symbol=args.market_symbol or None,
    )

    timeframe = fetch_mod.normalize_timeframe(args.timeframe)
    tf_seconds = fetch_mod.timeframe_to_seconds(timeframe)
    tf_ms = fetch_mod.timeframe_to_milliseconds(timeframe)

    db_base = Path(args.db).resolve()
    fast_base = Path(args.fast_out).resolve() if args.fast_out else None

    print(f"[CFG] ticks_dir={ticks_dir}")
    print(f"[CFG] discovered_files={len(all_files)} selected_files={len(files)}")
    print(f"[CFG] timeframe={timeframe} start={fmt_ms(start_ms)} end_exclusive={fmt_ms(end_ms_excl)}")
    print(f"[CFG] months={sorted(exact_months) if exact_months else '-'} month_from={args.month_from or '-'} month_to={args.month_to or '-'}")
    print(f"[CFG] per_month={args.per_month}")

    if args.per_month:
        grouped: Dict[str, List[Path]] = defaultdict(list)
        for p in files:
            meta = parse_file_meta(p)
            month_token = meta["month"] if meta else "unknown"
            grouped[month_token].append(p)

        for month_token in sorted(grouped):
            month_db = path_with_month(db_base, month_token)
            month_fast = path_with_month(fast_base, month_token) if fast_base else None
            print(f"[MONTH] {month_token} files={len(grouped[month_token])}")
            process_batch(
                files=grouped[month_token],
                start_ms=start_ms,
                end_ms_excl=end_ms_excl,
                timeframe=timeframe,
                tf_seconds=tf_seconds,
                tf_ms=tf_ms,
                market_symbol=market_symbol,
                fetch_mod=fetch_mod,
                db_path=month_db,
                fast_out=month_fast,
                fast_dep=fast_dep,
                fresh=args.fresh,
            )
    else:
        process_batch(
            files=files,
            start_ms=start_ms,
            end_ms_excl=end_ms_excl,
            timeframe=timeframe,
            tf_seconds=tf_seconds,
            tf_ms=tf_ms,
            market_symbol=market_symbol,
            fetch_mod=fetch_mod,
            db_path=db_base,
            fast_out=fast_base,
            fast_dep=fast_dep,
            fresh=args.fresh,
        )


if __name__ == "__main__":
    main()
