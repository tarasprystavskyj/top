#!/usr/bin/env python
"""Build a compact synthetic HYPE live session from local artifacts.

The output is intentionally UI-facing and compact. It does not copy raw
run_telemetry_*.jsonl, live_stdout_*.log, NPZ blobs, or live runtime state.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LIVE_ROOT = REPO_ROOT / "obw_platform" / "_reports" / "_live"
DEFAULT_OUTPUT = DEFAULT_LIVE_ROOT / "hype_consolidated"
DEFAULT_MAX_POINTS = 5000
DEFAULT_MAX_TELEMETRY_BYTES = 20 * 1024 * 1024
_TELEMETRY_MARK_RE = re.compile(r'"mark"\s*:\s*(-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)')
_TELEMETRY_UTC_RE = re.compile(r'"utc"\s*:\s*"([^"]+)"')


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _to_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.isoformat()


def _safe_float(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except Exception:
        return None
    if not np.isfinite(out):
        return None
    return out


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _source_dirs(live_root: Path, output: Path, explicit: Optional[List[Path]] = None) -> List[Path]:
    if explicit:
        candidates = explicit
    else:
        candidates = [p for p in live_root.iterdir() if p.is_dir() and "hype" in p.name.lower()]
    out: List[Path] = []
    output_abs = output.resolve()
    for path in candidates:
        try:
            resolved = path.resolve()
        except Exception:
            continue
        if resolved == output_abs:
            continue
        if "consolidated" in path.name.lower():
            continue
        if not explicit and not _looks_like_hype_live_source(path):
            continue
        out.append(path)
    return sorted(out, key=lambda p: p.name.lower())


def _looks_like_hype_live_source(path: Path) -> bool:
    if "hype" not in path.name.lower():
        return False
    markers = (
        "RUN_STATUS.json",
        "STATUS.json",
        "chart.json",
        "live_equity.csv",
        "live_candles.csv",
        "live_chart_events.csv",
        "session.sqlite",
        "telemetry.jsonl",
        "ACTIVE_STATUS_PATH.txt",
    )
    return any((path / marker).exists() for marker in markers)


def _point(ts: Any, value: Any) -> Optional[Dict[str, Any]]:
    iso = _to_iso(ts)
    val = _safe_float(value)
    if not iso or val is None:
        return None
    return {"ts": iso, "value": float(val)}


def _read_series_csv(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        df = pd.read_csv(path)
    except Exception:
        return []
    if df.empty:
        return []
    ts_col = next((c for c in ("ts", "timestamp", "time", "dt", "datetime") if c in df.columns), None)
    value_col = next((c for c in ("value", "equity", "pnl", "cum_pnl", "realized_pnl", "realized_value") if c in df.columns), None)
    if not ts_col or not value_col:
        return []
    out: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        item = _point(row.get(ts_col), row.get(value_col))
        if item:
            out.append(item)
    return sorted(out, key=lambda x: x["ts"])


def _read_candles_csv(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        df = pd.read_csv(path)
    except Exception:
        return []
    if df.empty or "ts" not in df.columns:
        return []
    value_cols = [c for c in ("open", "high", "low", "close") if c in df.columns]
    if not value_cols:
        return []
    df = df.copy()
    df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    for col in value_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["ts", *value_cols]).sort_values("ts")
    if df.empty:
        return []
    df["bucket"] = df["ts"].dt.floor("min")
    rows: List[Dict[str, Any]] = []
    for bucket, group in df.groupby("bucket", sort=True):
        opens = group["open"] if "open" in group.columns else group[value_cols[0]]
        highs = group["high"] if "high" in group.columns else group[value_cols[0]]
        lows = group["low"] if "low" in group.columns else group[value_cols[0]]
        closes = group["close"] if "close" in group.columns else group[value_cols[0]]
        row = {
            "ts": bucket.isoformat(),
            "open": float(opens.iloc[0]),
            "high": float(highs.max()),
            "low": float(lows.min()),
            "close": float(closes.iloc[-1]),
        }
        if all(np.isfinite(row[k]) for k in ("open", "high", "low", "close")):
            rows.append(row)
    return rows


def _read_candles_npz(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = np.load(path, allow_pickle=True)
        timestamps = data["timestamp_s"]
        opens = data["open"]
        highs = data["high"]
        lows = data["low"]
        closes = data["close"]
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for idx in range(len(timestamps)):
        try:
            row = {
                "ts": pd.to_datetime(int(timestamps[idx]), unit="s", utc=True).floor("min").isoformat(),
                "open": float(opens[idx]),
                "high": float(highs[idx]),
                "low": float(lows[idx]),
                "close": float(closes[idx]),
            }
        except Exception:
            continue
        if all(np.isfinite(row[k]) for k in ("open", "high", "low", "close")):
            out.append(row)
    return out


def _read_mark_points_from_telemetry(path: Path) -> List[Dict[str, Any]]:
    if not path.exists() or path.stat().st_size <= 0:
        return []
    dedup: Dict[str, Dict[str, Any]] = {}
    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except Exception:
        return []
    with fh:
        for line in fh:
            if '"mark"' not in line:
                continue
            mark_match = _TELEMETRY_MARK_RE.search(line)
            utc_matches = list(_TELEMETRY_UTC_RE.finditer(line))
            if mark_match and utc_matches:
                point = _point(utc_matches[-1].group(1), mark_match.group(1))
            else:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                status = row.get("status") if isinstance(row, dict) and isinstance(row.get("status"), dict) else {}
                input_meta = row.get("input_meta") if isinstance(row, dict) and isinstance(row.get("input_meta"), dict) else {}
                if not input_meta and isinstance(status.get("input_meta"), dict):
                    input_meta = status.get("input_meta") or {}
                market = input_meta.get("market") if isinstance(input_meta.get("market"), dict) else {}
                point = _point(row.get("ts") or row.get("utc") or status.get("utc"), market.get("mark"))
            if point:
                dedup[point["ts"]] = point
    return sorted(dedup.values(), key=lambda x: x["ts"])


def _mark_points_to_minute_bars(points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    buckets: Dict[str, Dict[str, Any]] = {}
    for point in points:
        ts = pd.to_datetime(point.get("ts"), utc=True, errors="coerce")
        value = _safe_float(point.get("value"))
        if pd.isna(ts) or value is None:
            continue
        bucket = ts.floor("min").isoformat()
        existing = buckets.get(bucket)
        if not existing:
            buckets[bucket] = {"ts": bucket, "open": value, "high": value, "low": value, "close": value}
            continue
        existing["high"] = max(float(existing["high"]), value)
        existing["low"] = min(float(existing["low"]), value)
        existing["close"] = value
    return sorted(buckets.values(), key=lambda x: x["ts"])


def _merge_points(*series_groups: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_ts: Dict[str, Dict[str, Any]] = {}
    for group in series_groups:
        for row in group:
            ts = _to_iso(row.get("ts") or row.get("time") or row.get("timestamp"))
            value = _safe_float(row.get("value"))
            if ts and value is not None:
                by_ts[ts] = {"ts": ts, "value": float(value)}
    return sorted(by_ts.values(), key=lambda x: x["ts"])


def _merge_bars(*bar_groups: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_ts: Dict[str, Dict[str, Any]] = {}
    for group in bar_groups:
        for row in group:
            ts = _to_iso(row.get("ts") or row.get("time") or row.get("timestamp"))
            if not ts:
                continue
            vals = {key: _safe_float(row.get(key)) for key in ("open", "high", "low", "close")}
            if any(vals[key] is None for key in vals):
                continue
            by_ts[ts] = {"ts": ts, **{key: float(vals[key]) for key in vals}}  # type: ignore[arg-type]
    return sorted(by_ts.values(), key=lambda x: x["ts"])


def _downsample_points(points: List[Dict[str, Any]], max_points: int) -> List[Dict[str, Any]]:
    if len(points) <= max_points:
        return points
    if max_points < 3:
        return points[:max_points]
    step = int(np.ceil(len(points) / max_points))
    out = points[::step]
    if out[-1]["ts"] != points[-1]["ts"]:
        out.append(points[-1])
    return out[:max_points]


def _downsample_bars(bars: List[Dict[str, Any]], max_points: int) -> List[Dict[str, Any]]:
    if len(bars) <= max_points:
        return bars
    step = int(np.ceil(len(bars) / max_points))
    out: List[Dict[str, Any]] = []
    for idx in range(0, len(bars), step):
        chunk = bars[idx : idx + step]
        if not chunk:
            continue
        out.append(
            {
                "ts": chunk[0]["ts"],
                "open": float(chunk[0]["open"]),
                "high": float(max(float(row["high"]) for row in chunk)),
                "low": float(min(float(row["low"]) for row in chunk)),
                "close": float(chunk[-1]["close"]),
            }
        )
    return out[:max_points]


def _fill_synthetic_flat_bar_gaps(bars: List[Dict[str, Any]], max_gap_minutes: int = 60 * 72) -> Tuple[List[Dict[str, Any]], int]:
    if len(bars) < 2:
        return bars, 0
    out: List[Dict[str, Any]] = []
    inserted = 0
    for row in bars:
        if not out:
            out.append(row)
            continue
        prev = out[-1]
        prev_ts = pd.to_datetime(prev.get("ts"), utc=True, errors="coerce")
        row_ts = pd.to_datetime(row.get("ts"), utc=True, errors="coerce")
        close = _safe_float(prev.get("close"))
        if pd.notna(prev_ts) and pd.notna(row_ts) and close is not None:
            gap_minutes = int((row_ts - prev_ts).total_seconds() // 60)
            if 1 < gap_minutes <= max_gap_minutes:
                next_ts = prev_ts.floor("min") + pd.Timedelta(minutes=1)
                while next_ts < row_ts.floor("min"):
                    iso = next_ts.isoformat()
                    out.append({"ts": iso, "open": close, "high": close, "low": close, "close": close, "synthetic_gap_fill": True})
                    inserted += 1
                    next_ts += pd.Timedelta(minutes=1)
        out.append(row)
    return out, inserted


def _read_chart_snapshot(path: Path) -> Dict[str, List[Dict[str, Any]]]:
    data = _load_json(path)
    if not isinstance(data, dict):
        return {}
    out: Dict[str, List[Dict[str, Any]]] = {}
    for key in ("live", "backtest", "live_realized", "backtest_realized", "backtest_price", "distance", "mark", "price_bars", "markers", "labels", "price_lines"):
        rows = data.get(key)
        if isinstance(rows, list):
            out[key] = [row for row in rows if isinstance(row, dict)]
    return out


def _fetch_chart_snapshot(url: str) -> Dict[str, List[Dict[str, Any]]]:
    with urllib.request.urlopen(url, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, dict):
        return {}
    out: Dict[str, List[Dict[str, Any]]] = {}
    for key in ("live", "backtest", "live_realized", "backtest_realized", "backtest_price", "distance", "mark", "price_bars", "markers", "labels", "price_lines"):
        rows = data.get(key)
        if isinstance(rows, list):
            out[key] = [row for row in rows if isinstance(row, dict)]
    return out


def _compact_extra(raw: Any) -> str:
    if not raw:
        return "{}"
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return "{}"
    if not isinstance(data, dict):
        return "{}"
    fill = data.get("fill") if isinstance(data.get("fill"), dict) else {}
    closed = data.get("closed") if isinstance(data.get("closed"), dict) else {}
    out: Dict[str, Any] = {}
    if fill:
        out["fill"] = {
            key: fill.get(key)
            for key in ("fill_type", "live_fill_price", "expected_price", "qty", "side", "symbol", "utc", "reason")
            if fill.get(key) is not None
        }
    if closed:
        out["closed"] = {
            key: closed.get(key)
            for key in ("side", "avg_entry", "entry", "tp_price", "take_profit_price", "next_level_idx", "levels", "fills")
            if closed.get(key) is not None
        }
    return json.dumps(out, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _read_filled_orders_from_sqlite(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
    except Exception:
        return []
    try:
        tables = {row[0] for row in con.execute("select name from sqlite_master where type='table'")}
        if "orders" not in tables:
            return []
        cols = [row[1] for row in con.execute('pragma table_info("orders")')]
        order_col = next((c for c in ("ts_utc", "bar_time_utc", "created_at", "ts", "timestamp") if c in cols), None)
        sql = 'select * from "orders"'
        if "status" in cols:
            sql += " where upper(status) = 'FILLED'"
        if order_col:
            sql += f' order by "{order_col}" asc'
        rows: List[Dict[str, Any]] = []
        for row in con.execute(sql):
            item = dict(row)
            ts = _to_iso(item.get("ts_utc") or item.get("bar_time_utc") or item.get("timestamp"))
            price = _safe_float(item.get("price"))
            qty = _safe_float(item.get("qty"))
            if not ts or price is None or qty is None:
                continue
            rows.append(
                {
                    "order_id": str(item.get("order_id") or f"{path.parent.name}-{len(rows)}"),
                    "ts_utc": ts,
                    "bar_time_utc": _to_iso(item.get("bar_time_utc")) or ts,
                    "mode": str(item.get("mode") or "hype_consolidated"),
                    "symbol": str(item.get("symbol") or "HYPE-USDT"),
                    "side": str(item.get("side") or "LONG").upper(),
                    "type": str(item.get("type") or "OPEN").upper(),
                    "price": float(price),
                    "qty": float(qty),
                    "status": "FILLED",
                    "reason": str(item.get("reason") or ""),
                    "run_id": str(item.get("run_id") or path.parent.name),
                    "extra": _compact_extra(item.get("extra")),
                    "source_session": path.parent.name,
                }
            )
        return rows
    except Exception:
        return []
    finally:
        con.close()


def _read_open_positions_from_sqlite(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
    except Exception:
        return []
    try:
        tables = {row[0] for row in con.execute("select name from sqlite_master where type='table'")}
        if "open_positions" not in tables:
            return []
        cols = [row[1] for row in con.execute('pragma table_info("open_positions")')]
        select_cols = [c for c in ("bot_id", "symbol", "side", "qty", "entry", "tp_price", "sl_price", "ts_open", "run_id", "exchange", "timeframe", "status", "ts_close", "entry_fill", "entry_fill_ts", "exit_fill", "exit_fill_ts", "close_reason") if c in cols]
        rows = []
        for row in con.execute(f'select {",".join(select_cols)} from "open_positions"'):
            item = dict(row)
            item["source_session"] = path.parent.name
            rows.append(item)
        return rows
    except Exception:
        return []
    finally:
        con.close()


def _order_marker(row: Dict[str, Any], idx: int) -> Optional[Dict[str, Any]]:
    ts = _to_iso(row.get("ts_utc") or row.get("bar_time_utc"))
    price = _safe_float(row.get("price"))
    if not ts or price is None:
        return None
    order_type = str(row.get("type") or "").upper()
    reason = str(row.get("reason") or "").lower()
    kind = "meta_close" if order_type in {"CLOSE", "EXIT"} else "dca_buy"
    text = "Meta strategy full close" if kind == "meta_close" else "DCA buy"
    if "lead_open_position_detected" in reason:
        kind = "meta_open"
        text = "Meta strategy open"
    is_close = kind == "meta_close"
    return {
        "id": str(row.get("order_id") or f"order-{idx}"),
        "time": ts,
        "price": float(price),
        "text": text,
        "kind": kind,
        "layer": "events",
        "color": "#F87171" if is_close else "#38BDF8" if kind == "meta_open" else "#22C55E",
        "shape": "arrowDown" if is_close else "arrowUp",
        "position": "atPriceTop" if is_close else "atPriceBottom",
    }


def _read_event_csv(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        df = pd.read_csv(path)
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for idx, row in df.iterrows():
        ts = _to_iso(row.get("ts") or row.get("time"))
        price = _safe_float(row.get("price"))
        if not ts or price is None:
            continue
        event_type = str(row.get("type") or row.get("kind") or "")
        is_close = "close" in event_type.lower() or "exit" in event_type.lower()
        out.append(
            {
                "id": str(row.get("order_id") or row.get("id") or f"{path.parent.name}-event-{idx}"),
                "time": ts,
                "price": float(price),
                "text": "Meta strategy full close" if is_close else "DCA buy",
                "kind": "meta_close" if is_close else "dca_buy",
                "layer": "events",
                "color": "#F87171" if is_close else "#22C55E",
                "shape": "arrowDown" if is_close else "arrowUp",
                "position": "atPriceTop" if is_close else "atPriceBottom",
            }
        )
    return out


def _merge_markers(*groups: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_key: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for group in groups:
        for row in group:
            time_value = _to_iso(row.get("time") or row.get("ts"))
            price = _safe_float(row.get("price"))
            text = str(row.get("text") or row.get("kind") or "")
            if not time_value or price is None:
                continue
            item = dict(row)
            item["time"] = time_value
            item["price"] = float(price)
            marker_id = str(item.get("id") or "")
            key = ("id", marker_id, "") if marker_id else (time_value, f"{price:.8f}", text)
            by_key[key] = item
    return sorted(by_key.values(), key=lambda x: x["time"])


def _merge_labels(*groups: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_key: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for group in groups:
        for row in group:
            time_value = _to_iso(row.get("time") or row.get("ts"))
            price = _safe_float(row.get("price"))
            text = str(row.get("text") or "")
            if not time_value or price is None or not text:
                continue
            item = dict(row)
            item["time"] = time_value
            item["price"] = float(price)
            by_key[(time_value, f"{price:.8f}", text)] = item
    return sorted(by_key.values(), key=lambda x: (x["time"], str(x.get("id") or "")))[:80]


def _merge_price_lines(*groups: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for group in groups:
        for row in group:
            price = _safe_float(row.get("price"))
            text = str(row.get("text") or row.get("kind") or "")
            if price is None or not text:
                continue
            item = dict(row)
            item["price"] = float(price)
            by_key[(f"{price:.8f}", text)] = item
    return list(by_key.values())[:24]


def _write_csv(path: Path, rows: List[Dict[str, Any]], fieldnames: List[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _write_sqlite(path: Path, orders: List[Dict[str, Any]], positions: List[Dict[str, Any]]) -> None:
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    try:
        con.execute(
            """
            create table orders (
                order_id text primary key,
                ts_utc text,
                bar_time_utc text,
                mode text,
                symbol text,
                side text,
                type text,
                price real,
                qty real,
                status text,
                reason text,
                run_id text,
                extra text,
                source_session text
            )
            """
        )
        con.executemany(
            "insert or replace into orders values (:order_id,:ts_utc,:bar_time_utc,:mode,:symbol,:side,:type,:price,:qty,:status,:reason,:run_id,:extra,:source_session)",
            orders,
        )
        con.execute(
            """
            create table open_positions (
                bot_id text,
                symbol text,
                side text,
                qty real,
                entry real,
                tp_price real,
                sl_price real,
                ts_open text,
                run_id text,
                exchange text,
                timeframe text,
                status text,
                ts_close text,
                entry_fill real,
                entry_fill_ts text,
                exit_fill real,
                exit_fill_ts text,
                close_reason text,
                source_session text
            )
            """
        )
        normalized_positions = []
        for item in positions:
            normalized_positions.append({key: item.get(key) for key in ("bot_id", "symbol", "side", "qty", "entry", "tp_price", "sl_price", "ts_open", "run_id", "exchange", "timeframe", "status", "ts_close", "entry_fill", "entry_fill_ts", "exit_fill", "exit_fill_ts", "close_reason", "source_session")})
        con.executemany(
            "insert into open_positions values (:bot_id,:symbol,:side,:qty,:entry,:tp_price,:sl_price,:ts_open,:run_id,:exchange,:timeframe,:status,:ts_close,:entry_fill,:entry_fill_ts,:exit_fill,:exit_fill_ts,:close_reason,:source_session)",
            normalized_positions,
        )
        con.commit()
    finally:
        con.close()


def build_consolidated_session(
    live_root: Path,
    output: Path,
    sources: Optional[List[Path]] = None,
    server_chart_url: Optional[str] = None,
    max_points: int = DEFAULT_MAX_POINTS,
    force: bool = False,
) -> Dict[str, Any]:
    source_dirs = _source_dirs(live_root, output, sources)
    if output.exists() and any(output.iterdir()) and not force:
        raise SystemExit(f"Output exists and is not empty: {output}. Use --force to replace compact generated files.")
    output.mkdir(parents=True, exist_ok=True)

    chart_sources: List[Dict[str, List[Dict[str, Any]]]] = []
    live_series_groups: List[List[Dict[str, Any]]] = []
    backtest_groups: List[List[Dict[str, Any]]] = []
    backtest_price_groups: List[List[Dict[str, Any]]] = []
    backtest_realized_groups: List[List[Dict[str, Any]]] = []
    live_realized_groups: List[List[Dict[str, Any]]] = []
    bar_groups: List[List[Dict[str, Any]]] = []
    mark_groups: List[List[Dict[str, Any]]] = []
    marker_groups: List[List[Dict[str, Any]]] = []
    label_groups: List[List[Dict[str, Any]]] = []
    price_line_groups: List[List[Dict[str, Any]]] = []
    orders_by_id: Dict[str, Dict[str, Any]] = {}
    positions: List[Dict[str, Any]] = []
    strategy_params: Optional[Any] = None
    skipped_large_telemetry: List[str] = []

    for session_dir in source_dirs:
        chart = _read_chart_snapshot(session_dir / "chart.json")
        if chart:
            chart_sources.append(chart)
        live_series_groups.append(_read_series_csv(session_dir / "live_equity.csv"))
        backtest_groups.append(_read_series_csv(session_dir / "backtest_equity.csv"))
        backtest_price_groups.append(_read_series_csv(session_dir / "backtest_price.csv"))
        live_realized_groups.append(_read_series_csv(session_dir / "live_realized.csv"))
        backtest_realized_groups.append(_read_series_csv(session_dir / "backtest_realized.csv"))
        bar_groups.append(_read_candles_csv(session_dir / "live_candles.csv"))
        for npz in sorted(session_dir.glob("*ohlcv*.npz")):
            bar_groups.append(_read_candles_npz(npz))
        telemetry_points: List[Dict[str, Any]] = []
        for telemetry_path in sorted([*session_dir.glob("run_telemetry_*.jsonl"), session_dir / "telemetry.jsonl"]):
            if telemetry_path.exists() and telemetry_path.stat().st_size > DEFAULT_MAX_TELEMETRY_BYTES:
                skipped_large_telemetry.append(str(telemetry_path))
                continue
            telemetry_points.extend(_read_mark_points_from_telemetry(telemetry_path))
        if telemetry_points:
            telemetry_points = _merge_points(telemetry_points)
            mark_groups.append(telemetry_points)
            bar_groups.append(_mark_points_to_minute_bars(telemetry_points))
        marker_groups.append(_read_event_csv(session_dir / "live_chart_events.csv"))
        orders = _read_filled_orders_from_sqlite(session_dir / "session.sqlite")
        for idx, row in enumerate(orders):
            orders_by_id[str(row["order_id"])] = row
        marker_groups.append([m for idx, row in enumerate(orders) if (m := _order_marker(row, idx))])
        positions.extend(_read_open_positions_from_sqlite(session_dir / "session.sqlite"))
        if strategy_params is None and (session_dir / "live_strategy_params.json").exists():
            strategy_params = _load_json(session_dir / "live_strategy_params.json")

    if server_chart_url:
        remote_chart = _fetch_chart_snapshot(server_chart_url)
        if remote_chart:
            chart_sources.append(remote_chart)

    for chart in chart_sources:
        live_series_groups.append(chart.get("live", []))
        backtest_groups.append(chart.get("backtest", []))
        live_realized_groups.append(chart.get("live_realized", []))
        backtest_realized_groups.append(chart.get("backtest_realized", []))
        backtest_price_groups.append(chart.get("backtest_price", []))
        mark_groups.append(chart.get("mark", []))
        bar_groups.append(chart.get("price_bars", []))
        marker_groups.append(chart.get("markers", []))
        label_groups.append(chart.get("labels", []))
        price_line_groups.append(chart.get("price_lines", []))

    price_bars_raw = _merge_bars(*bar_groups)
    price_bars_full, synthetic_gap_fills = _fill_synthetic_flat_bar_gaps(price_bars_raw)
    mark_full = _merge_points(*mark_groups, [{"ts": row["ts"], "value": row["close"]} for row in price_bars_full])
    live_full = _merge_points(*live_series_groups)
    backtest_full = _merge_points(*backtest_groups)
    live_realized_full = _merge_points(*live_realized_groups)
    backtest_realized_full = _merge_points(*backtest_realized_groups)
    backtest_price_full = _merge_points(*backtest_price_groups)
    markers = _merge_markers(*marker_groups)
    labels = _merge_labels(*label_groups)
    price_lines = _merge_price_lines(*price_line_groups)

    chart_payload: Dict[str, Any] = {
        "schema": "hype_consolidated_chart_v1",
        "synthetic": True,
        "generated_at": _utc_now(),
        "source_sessions": [p.name for p in source_dirs],
        "sources": {
            "snapshot": "chart.json",
            "live": "live_equity.csv + source chart snapshots",
            "price_bars": "live_candles.csv + telemetry + source OHLCV/chart snapshots + synthetic flat gap fill",
            "mark": "telemetry.jsonl from consolidated price_bars close",
            "markers": "live_chart_events.csv + session.sqlite filled orders + source chart snapshots",
        },
        "warnings": [],
    }
    if synthetic_gap_fills:
        chart_payload["warnings"].append(
            f"Synthetic consolidated HYPE view uses {synthetic_gap_fills} flat price-bar gap fill(s); it is for visual inspection only, not edge validation."
        )
    if skipped_large_telemetry:
        chart_payload["warnings"].append(
            f"Skipped {len(skipped_large_telemetry)} large raw telemetry file(s) while building the compact synthetic view."
        )
    series_plan = {
        "live": _downsample_points(live_full, max_points),
        "backtest": _downsample_points(backtest_full, max_points),
        "live_realized": _downsample_points(live_realized_full, max_points),
        "backtest_realized": _downsample_points(backtest_realized_full, max_points),
        "backtest_price": _downsample_points(backtest_price_full, max_points),
        "mark": _downsample_points(mark_full, max_points),
        "price_bars": _downsample_bars(price_bars_full, max_points),
    }
    for key, rows in series_plan.items():
        if rows:
            chart_payload[key] = rows
    if markers:
        chart_payload["markers"] = markers
    if labels:
        chart_payload["labels"] = labels
    if price_lines:
        chart_payload["price_lines"] = price_lines

    counts_full = {
        "live": len(live_full),
        "backtest": len(backtest_full),
        "live_realized": len(live_realized_full),
        "backtest_realized": len(backtest_realized_full),
        "backtest_price": len(backtest_price_full),
        "mark": len(mark_full),
        "price_bars": len(price_bars_full),
        "price_bars_raw": len(price_bars_raw),
        "synthetic_gap_fills": synthetic_gap_fills,
        "markers": len(markers),
        "orders": len(orders_by_id),
        "positions": len(positions),
        "skipped_large_telemetry_files": len(skipped_large_telemetry),
    }
    chart_payload["consolidated_counts"] = counts_full
    downsampled = {
        key: {"from": counts_full[key], "to": len(rows)}
        for key, rows in series_plan.items()
        if counts_full.get(key, 0) > len(rows)
    }
    if downsampled:
        chart_payload["downsampled"] = {"max_points": max_points, "series": downsampled}

    _write_json(output / "chart.json", chart_payload)
    _write_csv(output / "live_equity.csv", chart_payload.get("live", []), ["ts", "value"])
    _write_csv(output / "live_candles.csv", chart_payload.get("price_bars", []), ["ts", "open", "high", "low", "close"])
    if chart_payload.get("backtest"):
        _write_csv(output / "backtest_equity.csv", chart_payload["backtest"], ["ts", "value"])
    if chart_payload.get("backtest_price"):
        _write_csv(output / "backtest_price.csv", chart_payload["backtest_price"], ["ts", "value"])
    event_rows = [
        {
            "ts": row.get("time"),
            "type": row.get("kind"),
            "side": "LONG",
            "symbol": "HYPE-USDT",
            "price": row.get("price"),
            "qty": "",
            "order_id": row.get("id"),
            "position_id": "",
            "pnl": 0.0,
        }
        for row in markers
    ]
    _write_csv(output / "live_chart_events.csv", event_rows, ["ts", "type", "side", "symbol", "price", "qty", "order_id", "position_id", "pnl"])
    with (output / "live_chart_events.jsonl").open("w", encoding="utf-8") as fh:
        for row in event_rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    with (output / "telemetry.jsonl").open("w", encoding="utf-8") as fh:
        for row in chart_payload.get("mark", []):
            fh.write(
                json.dumps(
                    {
                        "event": "poll",
                        "status": {
                            "schema": "hype_live_poll_compact_v1",
                            "utc": row["ts"],
                            "symbol": "HYPE-USDT",
                            "live_symbol": "HYPE/USDT:USDT",
                            "input_meta": {"market": {"mark": row["value"]}},
                        },
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )

    orders = sorted(orders_by_id.values(), key=lambda x: x.get("ts_utc") or "")
    _write_csv(output / "orders.csv", orders, ["order_id", "ts_utc", "bar_time_utc", "mode", "symbol", "side", "type", "price", "qty", "status", "reason", "run_id", "extra", "source_session"])
    _write_sqlite(output / "session.sqlite", orders, positions)

    if strategy_params is not None:
        _write_json(output / "live_strategy_params.json", strategy_params)
    data_started_at = min((row["ts"] for row in chart_payload.get("price_bars", []) if row.get("ts")), default=None)
    data_updated_at = max((row["ts"] for row in chart_payload.get("price_bars", []) if row.get("ts")), default=None)
    status_updated_at = chart_payload["generated_at"]
    status = {
        "schema": "hype_consolidated_status_v1",
        "status": "stopped",
        "synthetic": True,
        "symbol": "HYPE-USDT",
        "live_symbol": "HYPE/USDT:USDT",
        "exchange": "mixed",
        "live_exchange": "mixed",
        "timeframe": "1m",
        "started_at": data_started_at,
        "updated_at": status_updated_at,
        "utc": status_updated_at,
        "data_started_at": data_started_at,
        "data_updated_at": data_updated_at,
        "open_legs": 0,
        "filled_orders": len(orders),
        "source_sessions": [p.name for p in source_dirs],
        "notes": "Synthetic consolidated HYPE view; not evidence of strategy edge because source sessions used different strategy versions.",
    }
    _write_json(output / "RUN_STATUS.json", status)
    _write_json(output / "STATUS.json", status)
    (output / "ACTIVE_TELEMETRY_PATH.txt").write_text("telemetry.jsonl\n", encoding="utf-8")
    (output / "ACTIVE_STATUS_PATH.txt").write_text("RUN_STATUS.json\n", encoding="utf-8")
    (output / "ACTIVE_SESSION_DB_PATH.txt").write_text("session.sqlite\n", encoding="utf-8")
    (output / "ACTIVE_RUN_ID.txt").write_text("HYPE_CONSOLIDATED_SYNTHETIC\n", encoding="utf-8")

    manifest = {
        "generated_at": chart_payload["generated_at"],
        "output": str(output),
        "source_sessions": [str(p) for p in source_dirs],
        "server_chart_url_used": bool(server_chart_url),
        "counts_full": counts_full,
        "counts_output": {key: len(value) for key, value in chart_payload.items() if isinstance(value, list)},
        "skipped_large_telemetry_files": skipped_large_telemetry,
        "files": sorted(p.name for p in output.iterdir() if p.is_file()),
    }
    _write_json(output / "MANIFEST.json", manifest)
    return manifest


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-root", type=Path, default=DEFAULT_LIVE_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--source", action="append", type=Path, help="Explicit source session dir. Can be repeated.")
    parser.add_argument("--server-chart-url", default="", help="Optional public chart endpoint JSON to merge.")
    parser.add_argument("--max-points", type=int, default=DEFAULT_MAX_POINTS)
    parser.add_argument("--force", action="store_true", help="Replace generated compact output files if the output exists.")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    manifest = build_consolidated_session(
        live_root=args.live_root,
        output=args.output,
        sources=args.source,
        server_chart_url=args.server_chart_url or None,
        max_points=max(300, args.max_points),
        force=args.force,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
