from __future__ import annotations

import csv
import json
import math
import shutil
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
LIVE_ROOT = ROOT / "obw_platform" / "_reports" / "_live"
OUT = LIVE_ROOT / "hype_consolidated"
RUN_TAG = datetime.now(timezone.utc).strftime("%Y%m%d")
RUN_DATE = datetime.now(timezone.utc).date().isoformat()


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def iso(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, Any]], preferred_fields: list[str]) -> None:
    fields: list[str] = []
    for field in preferred_fields:
        if field not in fields:
            fields.append(field)
    for row in rows:
        for field in row:
            if field not in fields:
                fields.append(field)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})


def session_dirs() -> list[Path]:
    out: list[Path] = []
    candidates = [p for p in LIVE_ROOT.iterdir() if p.is_dir()]
    staging = LIVE_ROOT / "_server_pull_20260614"
    if staging.exists():
        candidates.extend([p for p in staging.iterdir() if p.is_dir()])
    for path in candidates:
        if not path.is_dir() or path.name == OUT.name:
            continue
        lower = path.name.lower()
        if "veronika" in lower or lower.startswith("hype_canary") or lower.startswith("hype_gateio"):
            if any((path / name).exists() for name in ("orders.csv", "telemetry.jsonl", "run_telemetry_20260525T213914Z.jsonl", "session.sqlite", "live_equity.csv")):
                out.append(path)
    return sorted(out, key=lambda p: p.name)


def merge_csv_file(name: str, key_fields: tuple[str, ...], preferred_fields: list[str], sources: list[Path]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_key: dict[tuple[str, ...], dict[str, Any]] = {}
    counts = {}
    for src in sources:
        rows = read_csv(src / name)
        counts[src.name] = len(rows)
        for row in rows:
            row = dict(row)
            row.setdefault("source_session", src.name)
            key = tuple(str(row.get(field, "")) for field in key_fields)
            if not any(key):
                key = tuple([src.name, str(len(by_key))])
            by_key[key] = row
    rows = list(by_key.values())
    if "ts" in preferred_fields:
        rows.sort(key=lambda r: parse_ts(str(r.get("ts") or r.get("ts_utc") or "")) or datetime.min.replace(tzinfo=timezone.utc))
    elif "ts_utc" in preferred_fields:
        rows.sort(key=lambda r: parse_ts(str(r.get("ts_utc") or r.get("ts") or "")) or datetime.min.replace(tzinfo=timezone.utc))
    return rows, {"input_counts": counts, "output_count": len(rows)}


def sqlite_table_rows(db_path: Path, table: str) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    try:
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        rows = [dict(row) for row in con.execute(f"select * from {table}")]
        con.close()
        return rows
    except Exception:
        return []


def merge_orders(sources: list[Path]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_key: dict[tuple[str, ...], dict[str, Any]] = {}
    counts: dict[str, int] = {}
    for src in sources:
        rows = read_csv(src / "orders.csv")
        if not rows:
            rows = sqlite_table_rows(src / "session.sqlite", "orders")
        counts[src.name] = len(rows)
        for row in rows:
            row = dict(row)
            row.setdefault("source_session", src.name)
            key = (
                str(row.get("order_id") or ""),
                str(row.get("ts_utc") or row.get("ts") or ""),
                str(row.get("type") or row.get("action") or ""),
                str(row.get("price") or ""),
                str(row.get("qty") or ""),
            )
            if not any(key):
                key = (src.name, str(len(by_key)))
            if key not in by_key:
                by_key[key] = row
    rows = list(by_key.values())
    rows.sort(key=lambda r: parse_ts(str(r.get("ts_utc") or r.get("ts") or "")) or datetime.min.replace(tzinfo=timezone.utc))
    return rows, {"input_counts": counts, "output_count": len(rows)}


def iter_telemetry_files(src: Path) -> list[Path]:
    files = list(src.glob("*telemetry*.jsonl"))
    if (src / "telemetry.jsonl").exists():
        files.append(src / "telemetry.jsonl")
    return sorted(set(files))


def compact_telemetry(sources: list[Path]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_second: dict[str, dict[str, Any]] = {}
    per_source = {}
    for src in sources:
        n = 0
        for path in iter_telemetry_files(src):
            try:
                with path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        status = obj.get("status") if isinstance(obj, dict) else {}
                        utc = obj.get("utc") or (status or {}).get("utc")
                        if not utc and isinstance(status, dict):
                            utc = (((status.get("input_meta") or {}).get("market") or {}).get("utc"))
                        dt = parse_ts(utc)
                        if not dt:
                            continue
                        market = ((status or {}).get("input_meta") or {}).get("market") or {}
                        mark = market.get("mark")
                        if mark is None:
                            mark = market.get("cached_mark")
                        if mark is None:
                            symbol_marks = market.get("symbol_marks") or (status or {}).get("symbol_marks") or {}
                            if isinstance(symbol_marks, dict):
                                mark = next((v for v in symbol_marks.values() if v is not None), None)
                        if mark is None:
                            mark = obj.get("mark") or (status or {}).get("mark")
                        try:
                            mark_f = float(mark)
                        except Exception:
                            continue
                        key = dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
                        by_second[key] = {
                            "ts": key,
                            "mark": mark_f,
                            "symbol": obj.get("symbol") or (status or {}).get("symbol") or (status or {}).get("live_symbol") or "",
                            "source_session": src.name,
                            "source_file": path.name,
                        }
                        n += 1
            except OSError:
                continue
        per_source[src.name] = n
    rows = sorted(by_second.values(), key=lambda r: parse_ts(r["ts"]) or datetime.min.replace(tzinfo=timezone.utc))
    return rows, {"input_counts": per_source, "output_count": len(rows)}


def nearest_mark(telemetry: list[dict[str, Any]], dt: datetime | None) -> tuple[float | None, float | None]:
    if not dt or not telemetry:
        return None, None
    # Small dataset, linear scan is fine and keeps this script dependency-free.
    best = None
    best_abs = None
    for row in telemetry:
        t = parse_ts(str(row.get("ts") or ""))
        if not t:
            continue
        delta = abs((t - dt).total_seconds())
        if best_abs is None or delta < best_abs:
            best_abs = delta
            best = row
    if best is None:
        return None, None
    return float(best["mark"]), float(best_abs or 0.0)


def adverse_bp(expected: float, fill: float, side: str, action: str) -> float:
    if expected <= 0 or fill <= 0:
        return 0.0
    side = side.upper()
    action = action.upper()
    if action == "OPEN":
        return max(0.0, (fill - expected) / expected * 10000.0) if side == "LONG" else max(0.0, (expected - fill) / expected * 10000.0)
    return max(0.0, (expected - fill) / expected * 10000.0) if side == "LONG" else max(0.0, (fill - expected) / expected * 10000.0)


def signed_bp(expected: float, fill: float, side: str, action: str) -> float:
    if expected <= 0 or fill <= 0:
        return 0.0
    side = side.upper()
    action = action.upper()
    sign = 1.0 if ((side == "LONG" and action == "OPEN") or (side == "SHORT" and action != "OPEN")) else -1.0
    return sign * (fill - expected) / expected * 10000.0


def extract_fill_observations(orders: list[dict[str, Any]], telemetry: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in orders:
        if str(row.get("status", "")).upper() != "FILLED":
            continue
        side = str(row.get("side") or "LONG")
        action = str(row.get("type") or row.get("action") or "OPEN")
        ts = parse_ts(str(row.get("ts_utc") or row.get("ts") or ""))
        extra = read_extra(row.get("extra"))
        candidates: list[dict[str, Any]] = []
        if isinstance(extra.get("fill"), dict):
            candidates.append(extra["fill"])
        closed = extra.get("closed")
        if isinstance(closed, dict):
            for fill in closed.get("fills") or []:
                if isinstance(fill, dict):
                    candidates.append(fill)
        if not candidates:
            candidates.append(row)
        for fill in candidates:
            expected = first_float(fill, ["expected_price", "requested_price", "price"], row.get("price"))
            live_fill = first_float(fill, ["live_fill_price", "fill_price", "average", "price"], row.get("price"))
            qty = first_float(fill, ["qty", "amount"], row.get("qty"))
            fill_type = str(fill.get("fill_type") or row.get("reason") or "")
            fill_dt = parse_ts(str(fill.get("fill_dt") or fill.get("utc") or row.get("ts_utc") or ""))
            mark_near, mark_lag_sec = nearest_mark(telemetry, fill_dt or ts)
            basis_expected = expected
            if expected <= 0 or live_fill <= 0:
                continue
            obs = {
                "ts_utc": iso(fill_dt or ts),
                "source_order_ts": row.get("ts_utc") or row.get("ts") or "",
                "source_session": row.get("source_session") or "",
                "symbol": fill.get("symbol") or row.get("symbol") or "",
                "side": side,
                "action": action,
                "fill_type": fill_type,
                "reason": fill.get("reason") or row.get("reason") or "",
                "expected_price": expected,
                "live_fill_price": live_fill,
                "qty": qty,
                "notional_usdt": qty * live_fill if qty and live_fill else 0.0,
                "signed_slip_bp_expected": signed_bp(basis_expected, live_fill, side, action),
                "adverse_slip_bp_expected": adverse_bp(basis_expected, live_fill, side, action),
                "telemetry_mark_near": mark_near,
                "telemetry_mark_lag_sec": mark_lag_sec,
                "signed_slip_bp_mark": signed_bp(mark_near, live_fill, side, action) if mark_near else "",
                "adverse_slip_bp_mark": adverse_bp(mark_near, live_fill, side, action) if mark_near else "",
                "entry_lag_sec": first_float(fill, ["entry_lag_sec"], None),
                "raw_order_id": row.get("order_id") or "",
            }
            out.append(obs)
    seen = set()
    deduped = []
    for row in out:
        key = (row["ts_utc"], row["raw_order_id"], row["fill_type"], row["live_fill_price"], row["qty"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    deduped.sort(key=lambda r: parse_ts(str(r["ts_utc"])) or datetime.min.replace(tzinfo=timezone.utc))
    return deduped


def read_extra(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        obj = json.loads(value)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def first_float(obj: dict[str, Any], keys: list[str], fallback: Any = None) -> float:
    for key in keys:
        if key in obj and obj.get(key) not in (None, ""):
            try:
                return float(obj.get(key))
            except Exception:
                pass
    try:
        return float(fallback)
    except Exception:
        return 0.0


def summarize(values: list[float]) -> dict[str, float | int | None]:
    vals = sorted(float(v) for v in values if isinstance(v, (int, float)) and math.isfinite(float(v)))
    if not vals:
        return {"n": 0, "mean": None, "median": None, "p75": None, "p90": None, "p95": None, "max": None}
    def q(p: float) -> float:
        idx = min(len(vals) - 1, max(0, int(round((len(vals) - 1) * p))))
        return vals[idx]
    return {
        "n": len(vals),
        "mean": mean(vals),
        "median": median(vals),
        "p75": q(0.75),
        "p90": q(0.90),
        "p95": q(0.95),
        "max": max(vals),
    }


def fit_linear_model(obs: list[dict[str, Any]]) -> dict[str, Any]:
    vals = [float(o["adverse_slip_bp_mark"] if o["adverse_slip_bp_mark"] != "" else o["adverse_slip_bp_expected"]) for o in obs]
    expected_vals = [float(o["adverse_slip_bp_expected"]) for o in obs]
    mark_vals = [float(o["adverse_slip_bp_mark"]) for o in obs if o["adverse_slip_bp_mark"] != "" and float(o.get("telemetry_mark_lag_sec") or 9999) <= 10]
    by_type: dict[str, list[float]] = defaultdict(list)
    for o in obs:
        y = float(o["adverse_slip_bp_mark"] if o["adverse_slip_bp_mark"] != "" else o["adverse_slip_bp_expected"])
        by_type[str(o["fill_type"] or "unknown")].append(y)
    base = summarize(mark_vals or vals)["p75"]
    p95 = summarize(mark_vals or vals)["p95"]
    if base is None:
        base = 0.0
    if p95 is None:
        p95 = max(base, 1.0)
    return {
        "kind": "linear_bp",
        "source": f"hype_consolidated telemetry+live fills calibrated {RUN_DATE}",
        "base_bp": float(base),
        "coefficients": {
            "participation": 0.0,
            "range_bp": 0.0,
            "log_quote_volume": 0.0,
            "side_x_body_signed_bp": 0.0,
            "is_exit": 0.0,
        },
        "clip_min_bp": 0.0,
        "clip_max_bp": float(max(5.0, p95)),
        "sample_count": len(obs),
        "telemetry_mark_lag_lte_10s_count": len(mark_vals),
        "adverse_slip_expected_bp": summarize(expected_vals),
        "adverse_slip_telemetry_mark_bp": summarize(mark_vals),
        "adverse_slip_by_fill_type_bp": {k: summarize(v) for k, v in sorted(by_type.items())},
        "notes": [
            "Use base_bp as static per-side adverse slippage in legacy backtests.",
            "Telemetry-mark slippage is preferred when nearest mark is within 10 seconds of fill; expected-price slippage also includes signal/level drift.",
            "Coefficients are zero because the local consolidated artifact has no full orderbook microstructure series for robust feature fitting.",
        ],
    }


def copy_sqlite(srcs: list[Path]) -> str | None:
    latest = None
    latest_size = None
    for src in srcs:
        p = src / "session.sqlite"
        if not p.exists():
            continue
        size = p.stat().st_size
        if latest_size is None or size > latest_size:
            latest = p
            latest_size = size
    if not latest:
        return None
    shutil.copy2(latest, OUT / "session.sqlite")
    return str(latest)


def write_report(manifest: dict[str, Any], model: dict[str, Any], obs: list[dict[str, Any]]) -> None:
    lines = [
        "# HYPE Consolidated Veronika Telemetry Slippage Calibration",
        "",
        f"Generated: {manifest['generated_at']}",
        f"Source sessions: {len(manifest['source_sessions'])}",
        f"Telemetry points: {manifest['telemetry']['output_count']}",
        f"Orders: {manifest['orders']['output_count']}",
        f"Fill observations: {len(obs)}",
        "",
        "## Recommended Backtest Model",
        "",
        "```json",
        json.dumps(model, ensure_ascii=False, indent=2, sort_keys=True),
        "```",
        "",
        "## Interpretation",
        "",
        "- `expected_price` slippage measures strategy level vs fill and can include delayed signal/level movement.",
        "- nearest telemetry mark is the better proxy for actual market execution error, when lag is small.",
        "- if server cleanup is planned, keep `telemetry.jsonl`, `orders.csv`, `live_equity.csv`, `live_candles.csv`, `session.sqlite`, and this calibration JSON.",
    ]
    (OUT / f"SLIPPAGE_TELEMETRY_CALIBRATION_{RUN_TAG}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def dedupe_chart_events(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Collapse cross-source duplicate chart-event markers.

    A single logical fill is recorded once per source session - the same
    lead-trader trade is mirrored across exchanges (htx/gateio/bingx/mexc) and
    across repeated snapshots of one live run. Each copy lands with a slightly
    different sub-second timestamp and per-exchange fill price, so a key of
    (ts, type, price, text) never collapses them and the chart shows 2-4 arrows
    stacked on one candle ("doubled" markers).

    They are the same logical event when they share
    (minute bucket, type, side, symbol). Keeping side+symbol in the key
    preserves both legs of dual-direction (long+short) strategies and never
    merges two genuinely distinct events from the same source. Rows are expected
    pre-sorted by ts ascending (merge_csv_file does this), so the earliest copy
    per key is the one kept.
    """
    seen: set = set()
    out: list[dict[str, Any]] = []
    dropped = 0
    for row in rows:
        dt = parse_ts(str(row.get("ts") or row.get("ts_utc") or ""))
        minute_bucket = int(dt.timestamp() // 60) if dt else None
        etype = str(row.get("type") or "")
        side = str(row.get("side") or "").upper()
        symbol = str(row.get("symbol") or "").upper()
        if side or symbol:
            key: tuple[Any, ...] = (minute_bucket, etype, side, symbol)
        else:
            # No side/symbol (older event format): keep price in the key so we
            # never over-collapse distinct events.
            key = (minute_bucket, etype, side, symbol, str(row.get("price") or ""))
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        out.append(row)
    return out, dropped


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    sources = session_dirs()
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    telemetry, telemetry_meta = compact_telemetry(sources)
    write_csv(OUT / "telemetry_marks.csv", telemetry, ["ts", "mark", "symbol", "source_session", "source_file"])
    with (OUT / "telemetry.jsonl").open("w", encoding="utf-8") as f:
        for row in telemetry:
            f.write(json.dumps({"event": "poll", "status": {"utc": row["ts"], "input_meta": {"market": {"mark": row["mark"]}}, "symbol": row["symbol"]}, "source_session": row["source_session"]}, ensure_ascii=False) + "\n")

    orders, orders_meta = merge_orders(sources)
    write_csv(OUT / "orders.csv", orders, ["order_id", "ts_utc", "bar_time_utc", "mode", "symbol", "side", "type", "price", "qty", "status", "reason", "run_id", "extra", "source_session"])

    for name, keys, fields in [
        ("live_equity.csv", ("ts",), ["ts", "value", "source_session"]),
        ("live_chart_events.csv", ("ts", "type", "price", "text"), ["ts", "type", "price", "text", "pnl", "source_session"]),
        ("live_candles.csv", ("ts",), ["ts", "open", "high", "low", "close", "volume", "source_session"]),
        ("backtest_equity.csv", ("ts",), ["ts", "value", "source_session"]),
        ("backtest_price.csv", ("ts",), ["ts", "value", "source_session"]),
    ]:
        rows, _meta = merge_csv_file(name, keys, fields, sources)
        if name == "live_chart_events.csv" and rows:
            rows, dropped = dedupe_chart_events(rows)
            print(f"live_chart_events.csv: collapsed {dropped} cross-source duplicate markers, kept {len(rows)}")
        if rows:
            write_csv(OUT / name, rows, fields)

    sqlite_source = copy_sqlite(sources)
    observations = extract_fill_observations(orders, telemetry)
    write_csv(
        OUT / "slippage_observations_telemetry.csv",
        observations,
        [
            "ts_utc",
            "source_order_ts",
            "source_session",
            "symbol",
            "side",
            "action",
            "fill_type",
            "reason",
            "expected_price",
            "live_fill_price",
            "qty",
            "notional_usdt",
            "signed_slip_bp_expected",
            "adverse_slip_bp_expected",
            "telemetry_mark_near",
            "telemetry_mark_lag_sec",
            "signed_slip_bp_mark",
            "adverse_slip_bp_mark",
            "entry_lag_sec",
            "raw_order_id",
        ],
    )
    model = fit_linear_model(observations)
    write_json(OUT / f"dynamic_slippage_model_telemetry_{RUN_TAG}.json", model)
    write_json(OUT / "dynamic_slippage_model.json", model)

    manifest = {
        "generated_at": generated_at,
        "output": str(OUT),
        "source_sessions": [str(p) for p in sources],
        "telemetry": telemetry_meta,
        "orders": orders_meta,
        "fill_observations": len(observations),
        "sqlite_source": sqlite_source,
        "server_pull_status": f"staged_from_vps2_{RUN_TAG}" if (LIVE_ROOT / "_server_pull_20260614").exists() else "not_pulled_this_run_auth_required",
        "important_preserve_files": [
            "telemetry.jsonl",
            "telemetry_marks.csv",
            "orders.csv",
            "slippage_observations_telemetry.csv",
            "dynamic_slippage_model.json",
            "live_equity.csv",
            "live_candles.csv",
            "session.sqlite",
        ],
    }
    write_json(OUT / f"CONSOLIDATION_MANIFEST_{RUN_TAG}.json", manifest)
    write_report(manifest, model, observations)

    print(json.dumps({"output": str(OUT), "sessions": len(sources), "telemetry": len(telemetry), "orders": len(orders), "fill_observations": len(observations), "model_base_bp": model["base_bp"], "model_clip_max_bp": model["clip_max_bp"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
