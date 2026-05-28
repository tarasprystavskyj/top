#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Collect 1m OHLCV only around Telegram signal event windows.

This is a research/data-collection helper. It does not import live runners,
daemons, broker clients, or order execution code.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import numpy as np

from collect_telegram_signal_1m_windows import (
    OHLCV_COLS,
    build_exchange,
    dt_to_ms,
    floor_minute,
    load_part_arrays,
    load_universe,
    merge_parts,
    resolve_linear_futures_market,
    rows_to_arrays,
    save_symbol_part,
    summarize_arrays,
    utc_now_iso,
    write_json,
    fetch_ohlcv_range,
)


ROOT = Path(__file__).resolve().parents[3]


def parse_iso_utc(raw: str) -> dt.datetime:
    value = dt.datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def normalize_base(raw: Any) -> str:
    s = str(raw or "").strip().upper().lstrip("#$")
    if "/" in s:
        return s.split("/", 1)[0]
    for quote in ("USDT", "USDC", "USD"):
        if s.endswith(quote) and len(s) > len(quote):
            return s[:-len(quote)]
    return s


def load_signal_times(path: Path) -> Tuple[List[Dict[str, Any]], List[str]]:
    rows: List[Dict[str, Any]] = []
    bases = set()
    with path.open("r", encoding="utf-8", newline="") as f:
        for i, row in enumerate(csv.DictReader(f)):
            if not row.get("dt_utc") or not row.get("symbol"):
                continue
            base = normalize_base(row.get("symbol"))
            when = parse_iso_utc(row["dt_utc"])
            rows.append({
                "idx": i,
                "base": base,
                "dt": when,
                "dt_utc": when.isoformat(),
                "message_idx": row.get("message_idx", i),
                "side": row.get("side", ""),
            })
            bases.add(base)
    rows.sort(key=lambda r: (r["dt"], r["idx"]))
    return rows, sorted(bases)


def merge_dt_windows(windows: Iterable[Tuple[dt.datetime, dt.datetime]]) -> List[Tuple[dt.datetime, dt.datetime]]:
    ordered = sorted(windows, key=lambda x: x[0])
    out: List[Tuple[dt.datetime, dt.datetime]] = []
    for start, end in ordered:
        if not out or start > out[-1][1]:
            out.append((start, end))
        else:
            out[-1] = (out[-1][0], max(out[-1][1], end))
    return out


def build_windows_by_symbol(
    signals: List[Dict[str, Any]],
    requested_symbols: List[str],
    warmup_hours: float,
    horizon_hours: float,
) -> Dict[str, List[Tuple[dt.datetime, dt.datetime]]]:
    by_symbol: Dict[str, List[Tuple[dt.datetime, dt.datetime]]] = {s: [] for s in requested_symbols}
    warm = dt.timedelta(hours=float(warmup_hours))
    horizon = dt.timedelta(hours=float(horizon_hours))
    requested = set(requested_symbols)
    for sig in signals:
        base = sig["base"]
        if base not in requested:
            continue
        start = floor_minute(sig["dt"] - warm)
        end = floor_minute(sig["dt"] + horizon) + dt.timedelta(minutes=1)
        by_symbol[base].append((start, end))
    return {base: merge_dt_windows(wins) for base, wins in by_symbol.items() if wins}


def dedupe_rows(rows: List[List[float]]) -> List[List[float]]:
    by_ts: Dict[int, List[float]] = {}
    for row in rows:
        by_ts[int(row[0])] = row
    return [by_ts[k] for k in sorted(by_ts)]


def write_handoff(path: Path, meta: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Telegram 1m Event Windows Collection Handoff",
        "",
        f"- status: {meta.get('status')}",
        f"- output_npz: `{meta.get('output_npz')}`",
        f"- metadata: `{meta.get('metadata_path')}`",
        f"- exchange/timeframe: `{meta.get('exchange')}` / `{meta.get('timeframe')}`",
        f"- warmup_hours: `{meta.get('warmup_hours')}`",
        f"- horizon_hours: `{meta.get('horizon_hours')}`",
        f"- signal_rows: `{meta.get('signal_rows')}`",
        f"- requested_symbols: `{len(meta.get('requested_symbols', []))}`",
        f"- fetched_symbols: `{len(meta.get('fetched_base_symbols', []))}`",
        f"- missing_symbols: `{', '.join(meta.get('missing_symbols', []))}`",
        "",
        "See metadata JSON for per-symbol windows, bars, requests, and failures.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def callback(session_id: str, message: str) -> None:
    if not session_id:
        return
    try:
        subprocess.run(
            ["codex.cmd", "exec", "resume", session_id, message],
            cwd=str(ROOT),
            timeout=120,
            check=False,
        )
    except Exception:
        pass


def write_agent_event(meta: Dict[str, Any]) -> None:
    events_dir = ROOT / ".agent" / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    safe_status = str(meta.get("status") or "unknown").replace(" ", "_")
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = events_dir / f"telegram_1m_event_windows_{safe_status}_{ts}.md"
    lines = [
        "# Telegram Event Window Collection",
        "",
        f"status: {meta.get('status')}",
        f"output_npz: {meta.get('output_npz')}",
        f"metadata: {meta.get('metadata_path')}",
        f"handoff: {meta.get('handoff_path')}",
        f"symbols: {meta.get('output_symbols_count')}",
        f"rows: {meta.get('output_rows')}",
        f"missing_symbols: {', '.join(meta.get('missing_symbols') or [])}",
        "",
        "The top_event_watchdog.py process can wake the Codex coordinator from this event.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals-csv", default=str(ROOT / "telegram_standard_bt_bundle/telegram_signal_standard_bt/telegram_signals_extracted.csv"))
    ap.add_argument("--universe-file", default=str(ROOT / "universe/telegram_signal_universe_all.txt"))
    ap.add_argument("--exchange", default="bingx")
    ap.add_argument("--timeframe", default="1m")
    ap.add_argument("--quotes", default="USDT")
    ap.add_argument("--warmup-hours", type=float, default=72.0)
    ap.add_argument("--horizon-hours", type=float, default=168.0)
    ap.add_argument("--out", default=str(ROOT / "DB/telegram_signals_1m_event_windows_bingx.npz"))
    ap.add_argument("--parts-dir", default=str(ROOT / "DB/telegram_signals_1m_event_windows_bingx_parts"))
    ap.add_argument("--metadata-out", default="")
    ap.add_argument("--handoff-out", default=str(ROOT / "obw_platform/meta_strategies/telegram_dca_mvp/reports/data_collection_1m_handoff.md"))
    ap.add_argument("--sleep-sec", type=float, default=0.12)
    ap.add_argument("--max-empty", type=int, default=3)
    ap.add_argument("--min-bars", type=int, default=1)
    ap.add_argument("--max-symbols", type=int, default=0)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--merge-only", action="store_true")
    ap.add_argument("--callback-session", default="")
    args = ap.parse_args()

    signals_csv = Path(args.signals_csv)
    universe_file = Path(args.universe_file)
    out_path = Path(args.out)
    parts_dir = Path(args.parts_dir)
    metadata_path = Path(args.metadata_out) if args.metadata_out else out_path.with_suffix(out_path.suffix + ".meta.json")
    handoff_path = Path(args.handoff_out)

    signals, signal_symbols = load_signal_times(signals_csv)
    requested_symbols = [s for s in load_universe(universe_file) if s in set(signal_symbols)]
    if args.max_symbols > 0:
        requested_symbols = requested_symbols[: args.max_symbols]
    windows_by_symbol = build_windows_by_symbol(signals, requested_symbols, args.warmup_hours, args.horizon_hours)
    quotes = tuple(q.strip().upper() for q in args.quotes.split(",") if q.strip())

    metadata: Dict[str, Any] = {
        "schema": "telegram_signal_1m_event_window_collection_v1",
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
        "status": "running",
        "signals_csv": str(signals_csv),
        "universe_file": str(universe_file),
        "signal_rows": len(signals),
        "signal_symbols": signal_symbols,
        "requested_symbols": requested_symbols,
        "exchange": args.exchange,
        "timeframe": args.timeframe,
        "warmup_hours": args.warmup_hours,
        "horizon_hours": args.horizon_hours,
        "output_npz": str(out_path),
        "metadata_path": str(metadata_path),
        "parts_dir": str(parts_dir),
        "handoff_path": str(handoff_path),
        "script": str(Path(__file__)),
        "command": " ".join(sys.argv),
        "per_symbol": {},
        "failures": {},
        "unresolved_symbols": [],
        "window_counts": {k: len(v) for k, v in windows_by_symbol.items()},
    }
    write_json(metadata_path, metadata)

    part_paths: List[Path] = []
    try:
        ex = None if args.merge_only else build_exchange(args.exchange)
        for idx, raw in enumerate(requested_symbols, start=1):
            part_path = parts_dir / f"{raw}_{args.exchange}_{args.timeframe}_event_windows.npz"
            if args.resume and part_path.exists():
                symbol, arrays = load_part_arrays(part_path)
                metadata["per_symbol"][raw] = {
                    "market": symbol,
                    "part": str(part_path),
                    **summarize_arrays(arrays),
                    "resumed": True,
                    "windows": len(windows_by_symbol.get(raw, [])),
                }
                part_paths.append(part_path)
                metadata["updated_at"] = utc_now_iso()
                write_json(metadata_path, metadata)
                print(f"[resume] {idx}/{len(requested_symbols)} {raw} part={part_path}", flush=True)
                continue
            if args.merge_only:
                if part_path.exists():
                    part_paths.append(part_path)
                continue
            wins = windows_by_symbol.get(raw, [])
            if not wins:
                continue
            market = resolve_linear_futures_market(ex, raw, quotes)
            if not market:
                metadata["unresolved_symbols"].append(raw)
                metadata["failures"][raw] = "unresolved linear futures market"
                metadata["updated_at"] = utc_now_iso()
                write_json(metadata_path, metadata)
                print(f"[skip] {idx}/{len(requested_symbols)} {raw} unresolved", flush=True)
                continue
            print(f"[fetch] {idx}/{len(requested_symbols)} {raw} market={market} windows={len(wins)}", flush=True)
            all_rows: List[List[float]] = []
            total_requests = 0
            for widx, (start, end) in enumerate(wins, start=1):
                rows, stats = fetch_ohlcv_range(
                    ex,
                    market,
                    args.timeframe,
                    dt_to_ms(start),
                    dt_to_ms(end),
                    sleep_sec=args.sleep_sec,
                    max_empty=args.max_empty,
                    progress_every_requests=0,
                )
                total_requests += int(stats.get("requests") or 0)
                all_rows.extend(rows)
                if widx % 10 == 0 or widx == len(wins):
                    print(f"[progress] {raw} window={widx}/{len(wins)} rows_raw={len(all_rows)}", flush=True)
            all_rows = dedupe_rows(all_rows)
            arrays = rows_to_arrays(all_rows)
            summary = summarize_arrays(arrays)
            if summary["bars"] < args.min_bars:
                metadata["failures"][raw] = f"too few bars: {summary['bars']}"
                metadata["updated_at"] = utc_now_iso()
                write_json(metadata_path, metadata)
                continue
            save_symbol_part(part_path, raw, market, arrays)
            part_paths.append(part_path)
            metadata["per_symbol"][raw] = {
                "market": market,
                "part": str(part_path),
                **summary,
                "windows": len(wins),
                "requests": total_requests,
            }
            metadata["updated_at"] = utc_now_iso()
            write_json(metadata_path, metadata)
            print(f"[ok] {idx}/{len(requested_symbols)} {market} bars={summary['bars']} windows={len(wins)}", flush=True)

        if part_paths:
            merge_info = merge_parts(out_path, part_paths)
            fetched_bases = sorted(metadata["per_symbol"].keys()) if metadata["per_symbol"] else [p.name.split("_", 1)[0] for p in part_paths]
            metadata.update({
                "status": "completed",
                "updated_at": utc_now_iso(),
                "fetched_symbols": merge_info["symbols"],
                "fetched_base_symbols": fetched_bases,
                "missing_symbols": sorted(set(requested_symbols) - set(fetched_bases)),
                "output_rows": merge_info["rows"],
                "output_symbols_count": len(merge_info["symbols"]),
            })
            write_json(metadata_path, metadata)
            write_handoff(handoff_path, metadata)
            print(f"[done] wrote {out_path} symbols={len(merge_info['symbols'])} rows={merge_info['rows']}", flush=True)
        else:
            metadata.update({
                "status": "failed",
                "updated_at": utc_now_iso(),
                "fetched_symbols": [],
                "fetched_base_symbols": [],
                "missing_symbols": requested_symbols,
                "output_rows": 0,
                "output_symbols_count": 0,
            })
            write_json(metadata_path, metadata)
            write_handoff(handoff_path, metadata)
            raise SystemExit("no symbol parts fetched")
    finally:
        if metadata_path.exists():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                write_handoff(handoff_path, metadata)
                write_agent_event(metadata)
                callback(
                    args.callback_session,
                    "Telegram 1m event-window collection finished. "
                    f"status={metadata.get('status')} npz={metadata.get('output_npz')} "
                    f"metadata={metadata_path} handoff={handoff_path} "
                    f"symbols={metadata.get('output_symbols_count')} rows={metadata.get('output_rows')}",
                )
            except Exception:
                pass


if __name__ == "__main__":
    main()
