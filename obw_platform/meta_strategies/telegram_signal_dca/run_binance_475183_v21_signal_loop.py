#!/usr/bin/env python3
"""Paper/backtest-only loop for Binance copy lead 4751838302089254401.

The loop treats Binance copy positions as external signals. V21 contributes the
single-leg DCA/exposure policy only; entry side is taken from the normalized
contrarian signal (`contrarian_side`) produced by the existing Binance copy
workflow. No live orders are placed and no secrets are read.
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import math
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import numpy as np
import requests
import yaml


ROOT = Path(__file__).resolve().parents[3]
MODULE_DIR = ROOT / "obw_platform" / "meta_strategies" / "telegram_signal_dca"
DEFAULT_LEAD = "4751838302089254401"
DEFAULT_BASE_REPORT = MODULE_DIR / "reports" / "night_tune_20260523_collect" / "binance_copy" / DEFAULT_LEAD
DEFAULT_OUT = MODULE_DIR / "reports" / "binance_475183_v21_signal_loop_20260523"
DEFAULT_CFG = ROOT / "obw_platform" / "configs" / "V21_strict_trend_stable_live_static9p38.yaml"
PHILOSOPHY_ZIP = ROOT.parent / "temp" / "doc_2026-05-21_15-12-55.claude.zip"
BINANCE_FAPI_KLINES = "https://fapi.binance.com/fapi/v1/klines"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_dt(raw: str) -> datetime:
    dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def safe_slug(raw: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in raw)[:120]


def run_cmd(cmd: List[str], log_path: Path, cwd: Path = ROOT, timeout_sec: float = 0.0) -> Dict[str, Any]:
    started = utc_now()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write("\n[%s] $ %s\n" % (started, " ".join(cmd)))
        log.flush()
        proc = subprocess.Popen(cmd, cwd=str(cwd), stdout=log, stderr=subprocess.STDOUT, text=True)
        try:
            rc = proc.wait(timeout=timeout_sec if timeout_sec > 0 else None)
            timed_out = False
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                rc = proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                rc = proc.wait()
            timed_out = True
    return {
        "cmd": cmd,
        "started_at": started,
        "finished_at": utc_now(),
        "returncode": rc,
        "timed_out": timed_out,
        "log": str(log_path),
    }


def load_positions(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as fp:
        rows = [dict(r) for r in csv.DictReader(fp)]
    return [
        r for r in rows
        if str(r.get("symbol") or "").upper().endswith("USDT")
        and str(r.get("contrarian_side") or "").upper() in {"LONG", "SHORT"}
        and r.get("closed_utc")
    ]


def position_window(rows: List[Dict[str, str]], warmup_days: int) -> Tuple[datetime, datetime]:
    opened = [parse_dt(str(r["opened_utc"])) for r in rows if r.get("opened_utc")]
    closed = [parse_dt(str(r["closed_utc"])) for r in rows if r.get("closed_utc")]
    start = min(opened) - timedelta(days=warmup_days)
    end = max(closed) + timedelta(days=4)
    now = datetime.now(timezone.utc)
    if end > now:
        end = now
    return start, end


def universe(rows: Iterable[Dict[str, str]]) -> List[str]:
    return sorted({str(r.get("symbol") or "").upper().strip() for r in rows if r.get("symbol")})


def fetch_klines_window(symbol: str, start: datetime, end: datetime, interval: str, sleep_sec: float) -> List[List[float]]:
    sess = requests.Session()
    rows: List[List[float]] = []
    cursor = ms(start)
    end_ms = ms(end)
    while cursor <= end_ms:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": cursor,
            "endTime": end_ms,
            "limit": 1500,
        }
        resp = sess.get(BINANCE_FAPI_KLINES, params=params, timeout=25)
        if resp.status_code == 429:
            time.sleep(max(2.0, sleep_sec * 20))
            continue
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        for item in batch:
            rows.append([
                int(item[0]) // 1000,
                float(item[1]),
                float(item[2]),
                float(item[3]),
                float(item[4]),
                float(item[5]),
            ])
        nxt = int(batch[-1][0]) + 60_000
        if nxt <= cursor:
            break
        cursor = nxt
        time.sleep(sleep_sec)
    dedup = {int(r[0]): r for r in rows}
    return [dedup[k] for k in sorted(dedup)]


def write_npz(path: Path, by_symbol: Dict[str, List[List[float]]]) -> None:
    symbols = list(by_symbol.keys())
    max_len = max([len(s) for s in symbols] + [1])
    offsets = [0]
    cols: Dict[str, List[np.ndarray]] = {k: [] for k in ("timestamp_s", "open", "high", "low", "close", "volume")}
    for symbol in symbols:
        arr = np.asarray(by_symbol[symbol], dtype=np.float64)
        cols["timestamp_s"].append(arr[:, 0].astype(np.int64))
        cols["open"].append(arr[:, 1].astype(np.float64))
        cols["high"].append(arr[:, 2].astype(np.float64))
        cols["low"].append(arr[:, 3].astype(np.float64))
        cols["close"].append(arr[:, 4].astype(np.float64))
        cols["volume"].append(arr[:, 5].astype(np.float64))
        offsets.append(offsets[-1] + len(arr))
    data: Dict[str, Any] = {
        "symbols": np.asarray(symbols, dtype=f"<U{max_len}"),
        "offsets": np.asarray(offsets, dtype=np.int64),
    }
    for key, parts in cols.items():
        data[key] = np.concatenate(parts) if parts else np.asarray([], dtype=np.float64)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, **data)


def philosophy_summary() -> List[str]:
    if not PHILOSOPHY_ZIP.exists():
        return ["consilium zip not found; using local loop defaults"]
    selected = [".claude/agents/orchestrator.md", ".claude/agents/brain-planning.md", ".claude/agents/brain-evaluation.md"]
    out = [
        "one wave at a time; maintain compact journal/status",
        "plan small bounded mutations plus at least one conservative candidate",
        "test candidates, rank by return with drawdown and margin-call penalties",
        "promote only if risk constraints remain acceptable",
        "human owns commits; loop writes reports and artifacts only",
    ]
    try:
        with zipfile.ZipFile(PHILOSOPHY_ZIP) as zf:
            found = [name for name in selected if name in zf.namelist()]
        out.append("source files: " + ", ".join(found))
    except Exception as exc:
        out.append("zip summary failed: %r" % (exc,))
    return out


def set_path(obj: Dict[str, Any], dotted: str, value: Any) -> None:
    cur: Dict[str, Any] = obj
    parts = dotted.split(".")
    for key in parts[:-1]:
        cur = cur.setdefault(key, {})
    cur[parts[-1]] = value


def get_path(obj: Dict[str, Any], dotted: str) -> Any:
    cur: Any = obj
    for key in dotted.split("."):
        cur = cur[key]
    return cur


def make_variants(base_cfg: Path, out_dir: Path) -> List[Dict[str, Any]]:
    base = yaml.safe_load(base_cfg.read_text(encoding="utf-8"))
    base.setdefault("research_notes", {})
    base["research_notes"]["binance_signal_loop"] = {
        "paper_backtest_only": True,
        "entry_side": "contrarian_side from Binance copy normalized position history",
        "single_leg": True,
        "trend_reaction": "disabled in signal simulator; trend/warmup are consilium/report context only",
    }
    variant_specs = [
        ("baseline", {}),
        ("conservative_wide_grid", {
            "strategy_params_long.drop1": 0.45,
            "strategy_params_long.drop2": 0.55,
            "strategy_params_short.rise1": 0.22,
            "strategy_params_short.rise2": 0.52,
        }),
        ("short_defensive_wide_rise", {
            "strategy_params_short.rise1": 0.30,
            "strategy_params_short.rise2": 0.65,
            "strategy_params_short.linearRisePercent": 0.26,
            "strategy_params_short.mult2": 1.6,
        }),
        ("long_defensive_wide_drop", {
            "strategy_params_long.drop1": 0.55,
            "strategy_params_long.drop2": 0.75,
            "strategy_params_long.linearDropPercent": 0.12,
            "strategy_params_long.mult2": 1.0,
        }),
        ("balanced_tighter_tp_grid", {
            "strategy_params_long.drop1": 0.25,
            "strategy_params_short.rise1": 0.16,
            "strategy_params_long.mult2": 1.35,
            "strategy_params_short.mult2": 1.35,
        }),
        ("aggressive_second_leg", {
            "strategy_params_long.mult2": 2.0,
            "strategy_params_short.mult2": 2.0,
            "strategy_params_long.drop1": 0.35,
            "strategy_params_short.rise1": 0.18,
        }),
    ]
    out: List[Dict[str, Any]] = []
    cfg_dir = out_dir / "configs"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    for name, changes in variant_specs:
        cfg = copy.deepcopy(base)
        for key, value in changes.items():
            if math.isfinite(float(value)):
                set_path(cfg, key, value)
        cfg_path = cfg_dir / ("%s.yaml" % safe_slug(name))
        cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=False), encoding="utf-8")
        out.append({"name": name, "cfg": str(cfg_path), "changes": changes})
    return out


def read_best_summary(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    best_label = ""
    best_score = -1e30
    for label, row in data.items():
        score = float(row.get("net_pct_per_30d") or row.get("net_pct") or -1e30)
        score -= abs(float(row.get("max_dd_pct") or 0.0)) * 0.03
        if score > best_score:
            best_label = label
            best_score = score
    return {"best_label": best_label, "score": best_score, "summary": data.get(best_label, {})}


def write_status(out_dir: Path, status: Dict[str, Any]) -> None:
    (out_dir / "STATUS.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Binance 475183 V21 Signal Loop",
        "",
        "Paper/backtest-only. No live orders. No `.env` secrets are read or printed.",
        "",
        "- Updated: `%s`" % utc_now(),
        "- tmux session: `%s`" % status.get("tmux_session", ""),
        "- Lead: `%s`" % status.get("lead_id", ""),
        "- Universe: `%s`" % ", ".join(status.get("universe", [])),
        "- Window with warmup: `%s` .. `%s`" % (status.get("window_start_utc", ""), status.get("window_end_utc", "")),
        "- Positions CSV: `%s`" % status.get("positions_csv", ""),
        "- NPZ: `%s`" % status.get("npz", ""),
        "- Latest wave: `%s`" % status.get("latest_wave", ""),
        "",
        "## Strategy Contract",
        "",
        "- Entry is single-leg only.",
        "- Entry side comes from Binance copy `contrarian_side` signal.",
        "- The simulator does not flip, block, or resize entry by trend.",
        "- Consilium philosophy is used for warmup/window discipline, bounded mutations, ranking, and promotion notes.",
        "",
        "## Consilium Notes",
        "",
    ]
    for item in status.get("philosophy", []):
        lines.append("- %s" % item)
    lines.extend(["", "## Variants", "", "| variant | best label | score | status |", "|---|---|---:|---|"])
    for item in status.get("variants", []):
        lines.append(
            "| %s | %s | %.6f | %s |"
            % (
                item.get("name", ""),
                item.get("best_label", ""),
                float(item.get("score", 0.0)),
                item.get("status", ""),
            )
        )
    (out_dir / "STATUS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_wave(args: argparse.Namespace, out_dir: Path, wave: int, status: Dict[str, Any]) -> Dict[str, Any]:
    wave_dir = out_dir / ("wave_%03d" % wave)
    wave_dir.mkdir(parents=True, exist_ok=True)
    positions_csv = Path(args.positions_csv)
    if args.refresh_positions:
        collect_dir = wave_dir / "position_refresh"
        result = run_cmd([
            sys.executable,
            str(MODULE_DIR / "binance_copy_contrarian_on_close.py"),
            "--portfolio-id",
            args.lead_id,
            "--time-range",
            args.time_range,
            "--page-size",
            str(args.page_size),
            "--max-pages",
            str(args.max_pages),
            "--out-dir",
            str(collect_dir),
            "--ttl-hours",
            str(args.ttl_hours),
            "--target-notional",
            str(args.target_notional),
            "--dca-counts",
            "0,1,2,3",
            "--exit-on-reversal",
            "--sleep-sec",
            str(args.sleep_sec),
        ], wave_dir / "refresh_positions.log", timeout_sec=args.step_timeout_sec)
        if result["returncode"] == 0:
            positions_csv = collect_dir / "position_history_normalized.csv"
        status.setdefault("steps", []).append({"wave": wave, "refresh_positions": result})

    rows = load_positions(positions_csv)
    if not rows:
        raise SystemExit("No usable Binance positions in %s" % positions_csv)
    symbols = universe(rows)
    start, end = position_window(rows, args.warmup_days)
    universe_file = out_dir / "universe_475183.txt"
    universe_file.write_text("\n".join(symbols) + "\n", encoding="utf-8")
    windows = {
        "lead_id": args.lead_id,
        "positions_csv": str(positions_csv),
        "symbols": symbols,
        "position_start_utc": min(str(r["opened_utc"]) for r in rows),
        "position_end_utc": max(str(r["closed_utc"]) for r in rows),
        "warmup_days": args.warmup_days,
        "window_start_utc": start.isoformat().replace("+00:00", "Z"),
        "window_end_utc": end.isoformat().replace("+00:00", "Z"),
    }
    (out_dir / "windows.json").write_text(json.dumps(windows, indent=2), encoding="utf-8")

    npz_path = out_dir / ("binance_%s_%s_%s_%s.npz" % (
        args.lead_id,
        args.timeframe,
        start.strftime("%Y%m%d"),
        end.strftime("%Y%m%d"),
    ))
    if args.replace_npz or not npz_path.exists():
        by_symbol: Dict[str, List[List[float]]] = {}
        for symbol in symbols:
            fetched = fetch_klines_window(symbol, start, end, args.timeframe, args.sleep_sec)
            if len(fetched) >= args.min_bars:
                by_symbol[symbol] = fetched
        if by_symbol:
            write_npz(npz_path, by_symbol)
        else:
            status.setdefault("notes", []).append("NPZ fetch produced no symbols")

    variants = make_variants(Path(args.v21_config), wave_dir)
    variant_rows: List[Dict[str, Any]] = []
    for variant in variants:
        variant_dir = wave_dir / "variants" / safe_slug(variant["name"])
        result = run_cmd([
            sys.executable,
            str(MODULE_DIR / "binance_copy_contrarian_on_close.py"),
            "--positions-csv",
            str(positions_csv),
            "--skip-fetch",
            "--out-dir",
            str(variant_dir),
            "--ttl-hours",
            str(args.ttl_hours),
            "--target-notional",
            str(args.target_notional),
            "--dca-counts",
            "0,1,2,3",
            "--exit-on-reversal",
            "--v21-config",
            variant["cfg"],
            "--sleep-sec",
            str(args.sleep_sec),
        ], variant_dir / "compare.log", timeout_sec=args.step_timeout_sec)
        best = read_best_summary(variant_dir / "summary.json")
        variant_rows.append({
            "name": variant["name"],
            "cfg": variant["cfg"],
            "changes": variant["changes"],
            "returncode": result["returncode"],
            "status": "ok" if result["returncode"] == 0 else "failed",
            "best_label": best.get("best_label", ""),
            "score": best.get("score", 0.0),
            "summary": best.get("summary", {}),
            "out_dir": str(variant_dir),
        })

    status.update({
        "updated_at": utc_now(),
        "lead_id": args.lead_id,
        "positions_csv": str(positions_csv),
        "universe": symbols,
        "universe_file": str(universe_file),
        "window_start_utc": windows["window_start_utc"],
        "window_end_utc": windows["window_end_utc"],
        "npz": str(npz_path),
        "latest_wave": wave,
        "variants": sorted(variant_rows, key=lambda x: float(x.get("score") or 0.0), reverse=True),
    })
    write_status(out_dir, status)
    return status


def main() -> None:
    ap = argparse.ArgumentParser(description="Run Binance 475183 signal-side V21 research loop.")
    ap.add_argument("--lead-id", default=DEFAULT_LEAD)
    ap.add_argument("--positions-csv", default=str(DEFAULT_BASE_REPORT / "position_history_normalized.csv"))
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--v21-config", default=str(DEFAULT_CFG))
    ap.add_argument("--tmux-session", default="binance_475183_v21_signal_loop")
    ap.add_argument("--time-range", default="365D")
    ap.add_argument("--page-size", type=int, default=100)
    ap.add_argument("--max-pages", type=int, default=5)
    ap.add_argument("--refresh-positions", action="store_true")
    ap.add_argument("--timeframe", default="1m")
    ap.add_argument("--warmup-days", type=int, default=21)
    ap.add_argument("--min-bars", type=int, default=1000)
    ap.add_argument("--replace-npz", action="store_true")
    ap.add_argument("--ttl-hours", type=float, default=72.0)
    ap.add_argument("--target-notional", type=float, default=100.0)
    ap.add_argument("--sleep-sec", type=float, default=0.08)
    ap.add_argument("--step-timeout-sec", type=float, default=0.0)
    ap.add_argument("--loop-sleep-sec", type=float, default=21600.0)
    ap.add_argument("--max-iterations", type=int, default=0, help="0 means run forever")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    status: Dict[str, Any] = {
        "started_at": utc_now(),
        "updated_at": utc_now(),
        "tmux_session": args.tmux_session,
        "paper_backtest_only": True,
        "philosophy": philosophy_summary(),
        "notes": [],
        "steps": [],
    }
    write_status(out_dir, status)

    wave = 1
    while True:
        try:
            status = run_wave(args, out_dir, wave, status)
        except Exception as exc:
            status.setdefault("errors", []).append({"wave": wave, "at": utc_now(), "error": repr(exc)})
            write_status(out_dir, status)
        if args.max_iterations and wave >= args.max_iterations:
            break
        wave += 1
        time.sleep(max(1.0, args.loop_sleep_sec))


if __name__ == "__main__":
    main()
