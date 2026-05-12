#!/usr/bin/env python3
"""Local JSON-tree UI server for the DEX orchestrator."""

from __future__ import annotations

import json
import os
import csv
import math
import hashlib
import html
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse


REPO_DIR = Path(__file__).resolve().parents[1]
STATIC_DIR = Path(__file__).resolve().parent / "static"
DATA_DIR = REPO_DIR / "ui_data"
CHART_CACHE_DIR = DATA_DIR / "chart_cache"
FULL_BACKTEST_DIR = DATA_DIR / "full_backtests"
BACKTEST_JOBS: dict[str, dict] = {}
BACKTEST_LOCK = threading.Lock()


def parse_float(value):
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def read_csv_rows(path: Path, limit: int | None = None) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(dict(row))
                if limit and len(rows) >= limit:
                    break
    except Exception:
        return []
    return rows


def row_metric(row: dict, *names: str):
    for name in names:
        if name in row and row.get(name) not in (None, ""):
            return row.get(name)
    return None


def monotonicity_score(points: list[dict]) -> float | None:
    values = [parse_float(p.get("equity")) for p in points]
    values = [v for v in values if v is not None and math.isfinite(v)]
    if len(values) < 3:
        return None
    increments = [values[i] - values[i - 1] for i in range(1, len(values))]
    total_move = sum(abs(x) for x in increments)
    if total_move <= 0:
        return 100.0
    negative_move = sum(abs(x) for x in increments if x < 0)
    peak = values[0]
    dd_area = 0.0
    peak_area = 0.0
    for value in values:
        peak = max(peak, value)
        dd_area += max(0.0, peak - value)
        peak_area += max(abs(peak), 1.0)
    downside_penalty = negative_move / total_move
    drawdown_penalty = min(dd_area / peak_area, 1.0)
    return round(100.0 * (1.0 - downside_penalty) * (1.0 - drawdown_penalty), 2)


def decision_log_points(path: Path) -> list[dict]:
    rows = read_csv_rows(path)
    points = []
    for idx, row in enumerate(rows):
        cash = parse_float(row.get("portfolio_cash_usd"))
        pos = parse_float(row.get("position_cash_usd"))
        fees = parse_float(row.get("fees_uncollected_usd"))
        equity = None
        if cash is not None or pos is not None or fees is not None:
            equity = (cash or 0.0) + (pos or 0.0) + (fees or 0.0)
        points.append(
            {
                "x": row.get("iso_time") or row.get("time") or row.get("timestamp") or str(idx),
                "equity": equity,
                "price": parse_float(row.get("price")),
                "action": row.get("action_taken") or row.get("action"),
                "route": row.get("active_route") or row.get("route"),
            }
        )
    return points


def ai_summary_for(metrics: dict) -> str:
    ret = parse_float(metrics.get("return_total_pct"))
    mdd = parse_float(metrics.get("mdd_total_pct"))
    strict = str(metrics.get("strict_pass")).lower() == "true"
    mono = parse_float(metrics.get("monotonicity_score"))
    parts = []
    if ret is not None:
        parts.append(f"return {ret:.2f}%")
    if mdd is not None:
        parts.append(f"MDD {mdd:.2f}%")
    if mono is not None:
        parts.append(f"monotonicity {mono:.1f}/100")
    if strict:
        verdict = "passes strict local gate"
    elif ret is not None and ret > 0:
        verdict = "positive but still needs gate review"
    else:
        verdict = "not yet a winner by current evidence"
    return f"{', '.join(parts) or 'limited metrics'}; {verdict}."


def candidate_from_summary(path: Path, row: dict) -> dict:
    decision_log = path.with_name("paper_live_decision_log.csv")
    points = decision_log_points(decision_log)
    metrics = {
        "strategy": row_metric(row, "strategy", "candidate", "name") or path.parent.name,
        "pool": row_metric(row, "pool_key", "pool", "symbol") or "unknown",
        "return_total_pct": row_metric(row, "return_total_pct", "return_pct", "apr_pct", "roi_pct"),
        "mdd_total_pct": row_metric(row, "mdd_total_pct", "max_drawdown_pct", "mdd_pct"),
        "vs_hodl50_usd": row_metric(row, "vs_hodl50_usd"),
        "strict_pass": row_metric(row, "strict_pass"),
        "score": row_metric(row, "score"),
        "time_from": row_metric(row, "time_from", "start", "from"),
        "time_to": row_metric(row, "time_to", "end", "to"),
        "events": row_metric(row, "events", "rows", "row_count"),
        "monotonicity_score": monotonicity_score(points),
    }
    rank_return = parse_float(metrics.get("return_total_pct")) or 0.0
    rank_score = parse_float(metrics.get("score")) or 0.0
    rank_mdd = abs(parse_float(metrics.get("mdd_total_pct")) or 0.0)
    rank_mono = parse_float(metrics.get("monotonicity_score")) or 0.0
    composite = rank_return + rank_score + rank_mono * 0.15 - rank_mdd * 1.5
    return {
        "id": str(path.parent.relative_to(REPO_DIR)).replace("\\", "/"),
        "source": str(path.relative_to(REPO_DIR)).replace("\\", "/"),
        "kind": "paper_live" if path.name == "paper_live_summary.csv" else "backtest_or_tune",
        "metrics": metrics,
        "ai_summary": ai_summary_for(metrics),
        "composite_score": round(composite, 4),
        "charts": {
            "paper_live": points[-250:],
            "backtest": [],
            "backtest_svg": chart_api_url(path, row),
            "full_backtest_start": full_backtest_api_url(path, row),
        },
    }


def chart_api_url(path: Path, row: dict) -> str:
    source = str(path.relative_to(REPO_DIR)).replace("\\", "/")
    strategy = str(row_metric(row, "strategy", "candidate", "name") or "")
    run_name = str(row_metric(row, "run_name") or "")
    return f"/api/chart?source={quote_component(source)}&strategy={quote_component(strategy)}&run_name={quote_component(run_name)}"


def full_backtest_api_url(path: Path, row: dict) -> str:
    source = str(path.relative_to(REPO_DIR)).replace("\\", "/")
    strategy = str(row_metric(row, "strategy", "candidate", "name") or "")
    run_name = str(row_metric(row, "run_name") or "")
    return f"/api/full-backtest/start?source={quote_component(source)}&strategy={quote_component(strategy)}&run_name={quote_component(run_name)}"


def quote_component(value: str) -> str:
    from urllib.parse import quote
    return quote(value, safe="")


def is_within_repo(path: Path) -> bool:
    try:
        path.resolve().relative_to(REPO_DIR.resolve())
        return True
    except Exception:
        return False


def find_summary_row(source: Path, strategy: str, run_name: str) -> dict:
    for row in read_csv_rows(source):
        if strategy and str(row_metric(row, "strategy", "candidate", "name") or "") != strategy:
            continue
        if run_name and str(row_metric(row, "run_name") or "") != run_name:
            continue
        return row
    rows = read_csv_rows(source, limit=1)
    return rows[0] if rows else {}


def nearby_curve_points(source: Path, row: dict) -> tuple[list[dict], str]:
    parent = source.parent
    candidates = list(parent.glob("*decision*.csv")) + list(parent.glob("*equity*.csv")) + list(parent.glob("*curve*.csv"))
    for path in candidates:
        points = decision_log_points(path)
        if len([p for p in points if p.get("equity") is not None or p.get("price") is not None]) >= 2:
            return points, f"curve file: {path.name}"
    return synthetic_backtest_points(row), "summary-derived: no local equity curve file found"


def synthetic_backtest_points(row: dict) -> list[dict]:
    initial = parse_float(row_metric(row, "initial_capital_usd", "capital_usd", "total_capital_usd")) or 100.0
    end = parse_float(row_metric(row, "equity_end_usd", "position_value_end_usd")) or initial
    mdd = abs(parse_float(row_metric(row, "mdd_pct", "mdd_total_pct", "max_drawdown_pct")) or 0.0) / 100.0
    trough = max(0.0, initial * (1.0 - mdd))
    values = [
        initial,
        initial * 1.01,
        initial * 0.995,
        max(trough, initial * 0.97),
        trough,
        (trough + end) * 0.55,
        (trough + end) * 0.70,
        (trough + end) * 0.85,
        end * 0.96,
        end,
    ]
    return [{"x": str(i), "equity": value, "price": None} for i, value in enumerate(values)]


def svg_polyline(points: list[dict], width: int, height: int) -> tuple[str, dict]:
    vals = [parse_float(p.get("equity")) if parse_float(p.get("equity")) is not None else parse_float(p.get("price")) for p in points]
    vals = [v for v in vals if v is not None and math.isfinite(v)]
    if len(vals) < 2:
        return "", {"min": None, "max": None}
    min_v, max_v = min(vals), max(vals)
    span = max(max_v - min_v, 1e-9)
    left, right, top, bottom = 54, width - 24, 44, height - 42
    coords = []
    for i, v in enumerate(vals):
        x = left + (right - left) * (i / max(len(vals) - 1, 1))
        y = bottom - (bottom - top) * ((v - min_v) / span)
        coords.append(f"{x:.1f},{y:.1f}")
    return " ".join(coords), {"min": min_v, "max": max_v}


def build_chart_svg(source: Path, row: dict, strategy: str, run_name: str) -> str:
    width, height = 900, 360
    points, source_label = nearby_curve_points(source, row)
    polyline, bounds = svg_polyline(points, width, height)
    title = strategy or row_metric(row, "strategy", "candidate", "name") or source.parent.name
    ret = row_metric(row, "return_pct", "return_total_pct", "apr_pct")
    mdd = row_metric(row, "mdd_pct", "mdd_total_pct", "max_drawdown_pct")
    hodl = row_metric(row, "hodl50_return_pct")
    score = row_metric(row, "score")
    source_rel = str(source.relative_to(REPO_DIR)).replace("\\", "/")
    subtitle = f"return={ret or '-'}% | mdd={mdd or '-'}% | hodl50={hodl or '-'}% | score={score or '-'}"
    esc = html.escape
    if not polyline:
        body = f'<text x="54" y="140" fill="#637083" font-size="18">No chart data available</text>'
    else:
        body = f'''
  <line x1="54" y1="44" x2="54" y2="{height-42}" stroke="#d9dee7"/>
  <line x1="54" y1="{height-42}" x2="{width-24}" y2="{height-42}" stroke="#d9dee7"/>
  <polyline points="{polyline}" fill="none" stroke="#1976d2" stroke-width="3" stroke-linejoin="round" stroke-linecap="round"/>
  <text x="54" y="{height-18}" fill="#637083" font-size="13">min {bounds["min"]:.4f} | max {bounds["max"]:.4f}</text>
'''
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <rect width="100%" height="100%" fill="#fbfcfe"/>
  <text x="24" y="26" fill="#17202a" font-size="18" font-family="Segoe UI, system-ui, sans-serif" font-weight="650">{esc(str(title))}</text>
  <text x="24" y="48" fill="#637083" font-size="13" font-family="Segoe UI, system-ui, sans-serif">{esc(str(subtitle))}</text>
  <text x="24" y="70" fill="#b06000" font-size="12" font-family="Segoe UI, system-ui, sans-serif">{esc(source_label)}</text>
  {body}
  <text x="54" y="{height-4}" fill="#637083" font-size="11" font-family="Segoe UI, system-ui, sans-serif">{esc(source_rel)}</text>
</svg>
'''


def chart_cache_key(source: Path, strategy: str, run_name: str) -> str:
    stat = source.stat()
    raw = f"{source.resolve()}|{stat.st_mtime_ns}|{stat.st_size}|{strategy}|{run_name}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def generate_or_read_chart(source_value: str, strategy: str, run_name: str) -> Path:
    source = (REPO_DIR / source_value).resolve()
    if not is_within_repo(source) or not source.exists() or source.suffix.lower() != ".csv":
        raise FileNotFoundError("invalid chart source")
    CHART_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CHART_CACHE_DIR / f"{chart_cache_key(source, strategy, run_name)}.svg"
    if cache.exists():
        return cache
    row = find_summary_row(source, strategy, run_name)
    svg = build_chart_svg(source, row, strategy, run_name)
    cache.write_text(svg, encoding="utf-8")
    return cache


def load_summary_meta(source: Path) -> dict:
    meta_path = source.with_name("summary.json")
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def backtest_job_id(source: Path, strategy: str, run_name: str) -> str:
    stat = source.stat()
    raw = f"full|{source.resolve()}|{stat.st_mtime_ns}|{source.stat().st_size}|{strategy}|{run_name}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def command_for_full_backtest(source: Path, row: dict, out_dir: Path) -> list[str]:
    meta = load_summary_meta(source)
    npz = meta.get("npz")
    if not npz:
        raise ValueError("summary.json has no npz field; cannot rerun full backtest")
    npz_path = (REPO_DIR / npz).resolve()
    if not is_within_repo(npz_path) or not npz_path.exists():
        raise FileNotFoundError(f"npz not found: {npz}")
    lower = row_metric(row, "lower_pct")
    upper = row_metric(row, "upper_pct")
    if lower in (None, "") or upper in (None, ""):
        raise ValueError("winner row has no lower_pct/upper_pct")
    time_from = row_metric(row, "time_from") or meta.get("time_from")
    time_to = row_metric(row, "time_to") or meta.get("time_to")
    if not time_from or not time_to:
        raise ValueError("winner row has no time_from/time_to")
    fee_scenario = row_metric(row, "fee_scenario") or "metadata"
    fee_rate = row_metric(row, "fee_rate") or 0.003
    rebalance_mode = row_metric(row, "rebalance_mode") or "periodic"
    rebalance_hours = row_metric(row, "rebalance_hours") or 0
    capital = row_metric(row, "capital_usd", "initial_capital_usd") or 1000
    py = os.environ.get("DEX_BACKTEST_PYTHON", sys.executable)
    return [
        py,
        str(REPO_DIR / "dex_platform" / "backtest" / "cl_fee_replay_fast_npz_v3.py"),
        "--npz", str(npz_path),
        "--out-dir", str(out_dir),
        "--fee-rates", f"{fee_scenario}:{fee_rate}",
        "--time-from", str(time_from),
        "--time-to", str(time_to),
        "--capital-mode", "fixed",
        "--initial-capital-usd", str(capital),
        "--total-capital-usd", str(row_metric(row, "total_capital_usd") or 1000),
        "--grid-lower", str(lower),
        "--grid-upper", str(upper),
        "--rebalance-grid", f"{rebalance_mode}:{rebalance_hours}",
        "--plots",
        "--max-plot-runs", "5",
        "--jobs", "1",
        "--no-fast-core",
    ]


def build_curve_svg(points: list[dict], source: Path, row: dict, strategy: str, label: str) -> str:
    width, height = 900, 360
    polyline, bounds = svg_polyline(points, width, height)
    title = strategy or row_metric(row, "strategy", "candidate", "name") or source.parent.name
    ret = row_metric(row, "return_pct", "return_total_pct", "apr_pct")
    mdd = row_metric(row, "mdd_pct", "mdd_total_pct", "max_drawdown_pct")
    esc = html.escape
    body = '<text x="54" y="140" fill="#637083" font-size="18">No curve data available</text>'
    if polyline:
        body = f'''
  <line x1="54" y1="44" x2="54" y2="{height-42}" stroke="#d9dee7"/>
  <line x1="54" y1="{height-42}" x2="{width-24}" y2="{height-42}" stroke="#d9dee7"/>
  <polyline points="{polyline}" fill="none" stroke="#188038" stroke-width="3" stroke-linejoin="round" stroke-linecap="round"/>
  <text x="54" y="{height-18}" fill="#637083" font-size="13">min {bounds["min"]:.4f} | max {bounds["max"]:.4f}</text>
'''
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <rect width="100%" height="100%" fill="#fbfcfe"/>
  <text x="24" y="26" fill="#17202a" font-size="18" font-family="Segoe UI, system-ui, sans-serif" font-weight="650">{esc(str(title))}</text>
  <text x="24" y="48" fill="#637083" font-size="13" font-family="Segoe UI, system-ui, sans-serif">return={esc(str(ret or '-'))}% | mdd={esc(str(mdd or '-'))}%</text>
  <text x="24" y="70" fill="#188038" font-size="12" font-family="Segoe UI, system-ui, sans-serif">{esc(label)}</text>
  {body}
</svg>
'''


def full_backtest_chart_from_output(out_dir: Path, source: Path, row: dict, strategy: str, run_name: str) -> Path:
    curves = out_dir / "curves.csv"
    if not curves.exists():
        raise FileNotFoundError("full backtest produced no curves.csv")
    points = []
    for csv_row in read_csv_rows(curves):
        if run_name and csv_row.get("run_name") not in ("", None, run_name):
            continue
        points.append(
            {
                "x": csv_row.get("datetime_utc") or csv_row.get("ts") or str(len(points)),
                "equity": parse_float(csv_row.get("equity")),
                "price": parse_float(csv_row.get("price")),
            }
        )
    if not points:
        for csv_row in read_csv_rows(curves):
            points.append(
                {
                    "x": csv_row.get("datetime_utc") or csv_row.get("ts") or str(len(points)),
                    "equity": parse_float(csv_row.get("equity")),
                    "price": parse_float(csv_row.get("price")),
                }
            )
    chart = out_dir / "full_backtest_equity.svg"
    chart.write_text(build_curve_svg(points, source, row, strategy, "full backtest curve"), encoding="utf-8")
    return chart


def run_full_backtest_job(job_id: str, source: Path, strategy: str, run_name: str) -> None:
    with BACKTEST_LOCK:
        job = BACKTEST_JOBS[job_id]
        job.update({"status": "preparing", "progress_pct": 5, "message": "Preparing backtest command"})
    try:
        row = find_summary_row(source, strategy, run_name)
        out_dir = FULL_BACKTEST_DIR / job_id
        out_dir.mkdir(parents=True, exist_ok=True)
        cmd = command_for_full_backtest(source, row, out_dir)
        (out_dir / "command.json").write_text(json.dumps(cmd, ensure_ascii=False, indent=2), encoding="utf-8")
        with BACKTEST_LOCK:
            job.update({"status": "running", "progress_pct": 20, "message": "Backtest process running", "out_dir": str(out_dir.relative_to(REPO_DIR)).replace("\\", "/")})
        started = time.time()
        proc = subprocess.Popen(cmd, cwd=str(REPO_DIR), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
        while proc.poll() is None:
            elapsed = time.time() - started
            with BACKTEST_LOCK:
                job.update({"progress_pct": min(85, 20 + int(elapsed / 2)), "message": f"Running backtest ({int(elapsed)}s)"})
            time.sleep(1)
        stdout, stderr = proc.communicate(timeout=10)
        (out_dir / "stdout.log").write_text(stdout or "", encoding="utf-8")
        (out_dir / "stderr.log").write_text(stderr or "", encoding="utf-8")
        if proc.returncode != 0:
            with BACKTEST_LOCK:
                job.update({"status": "failed", "progress_pct": 100, "message": (stderr or stdout or f"returncode={proc.returncode}")[-1200:]})
            return
        chart = full_backtest_chart_from_output(out_dir, source, row, strategy, run_name)
        with BACKTEST_LOCK:
            job.update({"status": "succeeded", "progress_pct": 100, "message": "Full backtest chart ready", "chart_url": f"/api/full-backtest/chart?job_id={job_id}", "chart_path": str(chart.relative_to(REPO_DIR)).replace("\\", "/")})
    except Exception as exc:
        with BACKTEST_LOCK:
            job.update({"status": "failed", "progress_pct": 100, "message": f"{type(exc).__name__}: {exc}"})


def start_full_backtest(source_value: str, strategy: str, run_name: str) -> dict:
    source = (REPO_DIR / source_value).resolve()
    if not is_within_repo(source) or not source.exists() or source.suffix.lower() != ".csv":
        raise FileNotFoundError("invalid backtest source")
    job_id = backtest_job_id(source, strategy, run_name)
    existing_svg = FULL_BACKTEST_DIR / job_id / "full_backtest_equity.svg"
    with BACKTEST_LOCK:
        if job_id in BACKTEST_JOBS and BACKTEST_JOBS[job_id].get("status") not in ("failed", "missing"):
            return BACKTEST_JOBS[job_id]
        job = {
            "job_id": job_id,
            "status": "succeeded" if existing_svg.exists() else "queued",
            "progress_pct": 100 if existing_svg.exists() else 0,
            "message": "Cached full backtest chart ready" if existing_svg.exists() else "Queued",
            "chart_url": f"/api/full-backtest/chart?job_id={job_id}" if existing_svg.exists() else None,
            "source": source_value,
            "strategy": strategy,
            "run_name": run_name,
        }
        BACKTEST_JOBS[job_id] = job
        if existing_svg.exists():
            return job
    threading.Thread(target=run_full_backtest_job, args=(job_id, source, strategy, run_name), daemon=True).start()
    return job


def winners_payload() -> dict:
    candidates = []
    roots = [REPO_DIR / "DEX_REPORTS", REPO_DIR / "DEX_REPORTS_LOCAL"]
    names = ("paper_live_summary.csv", "summary.csv", "best_by_score.csv", "best_by_return.csv")
    for root in roots:
        if not root.exists():
            continue
        for name in names:
            for path in root.rglob(name):
                rows = read_csv_rows(path)
                if not rows:
                    continue
                for row in rows[:20]:
                    candidates.append(candidate_from_summary(path, row))
    deduped = {}
    for item in candidates:
        metrics = item.get("metrics", {})
        key = (
            metrics.get("strategy"),
            metrics.get("pool"),
            metrics.get("return_total_pct"),
            metrics.get("mdd_total_pct"),
        )
        current = deduped.get(key)
        if current is None or item["composite_score"] > current["composite_score"]:
            deduped[key] = item
    candidates = list(deduped.values())
    candidates.sort(key=lambda item: item["composite_score"], reverse=True)
    winners = candidates[:30]
    return {
        "schema_version": "dex_winners_v1",
        "generated_from": [str(r.relative_to(REPO_DIR)) for r in roots if r.exists()],
        "count": len(winners),
        "winners": winners,
        "notes": [
            "Monotonicity is estimated from available equity-like paper-live decision logs when present.",
            "Rows without decision-log curves keep monotonicity null and are ranked mostly by return/score/MDD.",
        ],
    }


def send_json(handler: SimpleHTTPRequestHandler, payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def fallback_tree(instance: str) -> dict:
    return {
        "schema_version": "orchestrator_tree_v1",
        "generated_at_utc": None,
        "summary": {
            "instance": instance,
            "worker_set": None,
            "cycle": 0,
            "phase": 1,
            "ready_for_live": False,
            "workers_total": 0,
            "jobs_total": 0,
            "running_jobs": [],
            "latest_succeeded_job": None,
            "server_check_requests": 0,
            "worker_data_requests": 0,
            "next_action": "waiting_for_orchestrator_export",
        },
        "tree": {
            "id": "orchestrator:empty",
            "type": "orchestrator",
            "label": "No orchestrator tree exported yet",
            "status": "empty",
            "children": [],
        },
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/tree":
            params = parse_qs(parsed.query)
            instance = params.get("instance", ["empathy"])[0]
            suffix = "" if instance == "main" else f"_{instance}"
            path = DATA_DIR / f"orchestrator_tree{suffix}.json"
            if path.exists():
                payload = path.read_text(encoding="utf-8-sig")
            else:
                payload = json.dumps(fallback_tree(instance), ensure_ascii=False, indent=2)
            send_json(self, json.loads(payload))
            return
        if parsed.path == "/api/winners":
            send_json(self, winners_payload())
            return
        if parsed.path == "/api/chart":
            params = parse_qs(parsed.query)
            source = params.get("source", [""])[0]
            strategy = params.get("strategy", [""])[0]
            run_name = params.get("run_name", [""])[0]
            try:
                chart_path = generate_or_read_chart(source, strategy, run_name)
                data = chart_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("X-Chart-Cache", str(chart_path.relative_to(REPO_DIR)).replace("\\", "/"))
                self.end_headers()
                self.wfile.write(data)
            except Exception as exc:
                send_json(self, {"error": f"{type(exc).__name__}: {exc}"})
            return
        if parsed.path == "/api/full-backtest/start":
            params = parse_qs(parsed.query)
            try:
                job = start_full_backtest(
                    params.get("source", [""])[0],
                    params.get("strategy", [""])[0],
                    params.get("run_name", [""])[0],
                )
                send_json(self, job)
            except Exception as exc:
                send_json(self, {"status": "failed", "progress_pct": 100, "message": f"{type(exc).__name__}: {exc}"})
            return
        if parsed.path == "/api/full-backtest/status":
            job_id = parse_qs(parsed.query).get("job_id", [""])[0]
            with BACKTEST_LOCK:
                job = dict(BACKTEST_JOBS.get(job_id) or {"job_id": job_id, "status": "missing", "progress_pct": 0, "message": "Unknown job"})
            send_json(self, job)
            return
        if parsed.path == "/api/full-backtest/chart":
            job_id = parse_qs(parsed.query).get("job_id", [""])[0]
            chart = FULL_BACKTEST_DIR / job_id / "full_backtest_equity.svg"
            if chart.exists():
                data = chart.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                send_json(self, {"error": "chart not found"})
            return
        if parsed.path == "/health":
            data = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        super().do_GET()


def main() -> int:
    host = os.environ.get("DEX_UI_HOST", "127.0.0.1")
    port = int(os.environ.get("DEX_UI_PORT", "8765"))
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CHART_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    FULL_BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"DEX orchestrator UI: http://{host}:{port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
