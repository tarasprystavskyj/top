#!/usr/bin/env python3
"""DarkKnight signal-side V21 single-leg research loop.

Backtest/tune only. This script never places live orders and never prints env
secrets. It refreshes DarkKnight signal history when an authorized Telegram
session is available, derives the symbol universe and calendar data window from
that signal history, collects OHLCV NPZ/SQLite data, then runs a small V21 grid
with one initial leg only. Trade side comes from each DarkKnight signal.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


ROOT = Path(__file__).resolve().parents[3]
MODULE_DIR = ROOT / "obw_platform" / "meta_strategies" / "telegram_signal_dca"
REPORT_ROOT = MODULE_DIR / "reports"
ENV_FILE = ROOT / ".env"
SOURCE_REPORT = REPORT_ROOT / "night_tune_20260523_collect" / "telegram" / "darkknighttrade"
SESSION_SRC = ROOT / "runs" / "telegram_paper" / "darkknight_session.session"
V21_CONFIG = ROOT / "obw_platform" / "configs" / "V21_strict_trend_stable_live_static9p38.yaml"


stop_requested = False


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def fmt_dt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_dt(raw: str) -> Optional[datetime]:
    s = str(raw or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def on_signal(signum: int, _frame: Any) -> None:
    global stop_requested
    stop_requested = True
    print("[stop] received signal %s; finishing current step" % signum, flush=True)


def safe_symbol(raw: str) -> str:
    s = str(raw or "").strip().upper()
    if "/" in s:
        return s.split("/", 1)[0]
    if s.endswith("USDT") and len(s) > 4:
        return s[:-4]
    return s


def run_cmd(cmd: List[str], log_path: Path, *, timeout_sec: float = 0.0) -> Dict[str, Any]:
    started = fmt_dt(utc_now())
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write("\n[%s] $ %s\n" % (started, " ".join(cmd)))
        log.flush()
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
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
        "finished_at": fmt_dt(utc_now()),
        "returncode": rc,
        "timed_out": timed_out,
        "log": str(log_path),
    }


def copy_seed_artifacts(out_dir: Path) -> Dict[str, Path]:
    raw_dir = out_dir / "data"
    raw_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "jsonl": raw_dir / "darkknighttrade_signals.jsonl",
        "csv": raw_dir / "darkknighttrade_signals.csv",
        "session_base": raw_dir / "darkknighttrade_session",
    }
    for name in ("jsonl", "csv"):
        src = SOURCE_REPORT / paths[name].name
        if src.exists() and not paths[name].exists():
            shutil.copy2(str(src), str(paths[name]))
    session_dst = Path(str(paths["session_base"]) + ".session")
    if not session_dst.exists():
        src = SOURCE_REPORT / "darkknighttrade_session.session"
        if src.exists():
            shutil.copy2(str(src), str(session_dst))
        elif SESSION_SRC.exists():
            shutil.copy2(str(SESSION_SRC), str(session_dst))
    return paths


def read_signal_rows(csv_path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not csv_path.exists() or csv_path.stat().st_size == 0:
        return rows
    with csv_path.open("r", encoding="utf-8", newline="") as fp:
        for row in csv.DictReader(fp):
            dt = parse_dt(str(row.get("dt_utc") or row.get("telegram_message_date") or ""))
            sym = safe_symbol(str(row.get("symbol") or ""))
            side = str(row.get("side") or "").strip().upper()
            if dt and sym and side in {"LONG", "SHORT"}:
                row["_dt"] = dt
                row["_base"] = sym
                rows.append(row)
    rows.sort(key=lambda r: r["_dt"])
    return rows


def write_universe(rows: Iterable[Dict[str, Any]], path: Path) -> List[str]:
    seen = set()
    out: List[str] = []
    for row in rows:
        base = str(row.get("_base") or "").strip().upper()
        if base and base not in seen:
            seen.add(base)
            out.append(base)
    path.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")
    return out


def timeframe_minutes(tf: str) -> int:
    unit = tf[-1]
    value = int(tf[:-1])
    if unit == "m":
        return value
    if unit == "h":
        return value * 60
    if unit == "d":
        return value * 1440
    raise ValueError("unsupported timeframe: %s" % tf)


def calc_bars(start: datetime, end: datetime, timeframe: str, warmup_days: int) -> int:
    start = start - timedelta(days=warmup_days)
    minutes = max(1.0, (end - start).total_seconds() / 60.0)
    bars = int(math.ceil(minutes / timeframe_minutes(timeframe))) + 1000
    return max(7200, bars)


def write_status(out_dir: Path, status: Dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "status.json").write_text(json.dumps(status, indent=2, default=str), encoding="utf-8")
    lines = [
        "# DarkKnight V21 Signal-Side Loop",
        "",
        "Backtest/tune only. No live orders. No secrets are printed.",
        "",
        "- Updated: `%s`" % status.get("updated_at", ""),
        "- Status: `%s`" % status.get("status", ""),
        "- tmux session: `%s`" % status.get("tmux_session", ""),
        "- Philosophy: consilium handles warmup/trend determination; execution is signal-side single-leg only; no trend reaction.",
        "- Channel: `darkknighttrade`",
        "- Source CSV: `%s`" % status.get("signals_csv", ""),
        "- Universe file: `%s`" % status.get("universe_file", ""),
        "- NPZ: `%s`" % status.get("npz", ""),
        "- Price DB: `%s`" % status.get("price_db", ""),
        "- Report dir: `%s`" % str(out_dir),
        "",
        "## Window",
        "",
        "- Signal start: `%s`" % status.get("signal_start_utc", ""),
        "- Signal end: `%s`" % status.get("signal_end_utc", ""),
        "- OHLCV warmup days: `%s`" % status.get("warmup_days", ""),
        "- Timeframe: `%s`" % status.get("timeframe", ""),
        "- Requested bars: `%s`" % status.get("bars", ""),
        "- TTL grid hours: `%s`" % ", ".join(str(x) for x in status.get("ttl_hours", [])),
        "- Entry mode grid: `%s`" % ", ".join(status.get("entry_modes", [])),
        "",
        "## Universe",
        "",
        "- Symbols: `%s`" % len(status.get("universe", [])),
        "",
        "```",
        " ".join(status.get("universe", [])),
        "```",
        "",
        "## Last Step",
        "",
        "```json",
        json.dumps(status.get("last_step", {}), indent=2),
        "```",
    ]
    (out_dir / "STATUS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def refresh_signals(paths: Dict[str, Path], out_dir: Path, args: argparse.Namespace, status: Dict[str, Any]) -> None:
    session_file = Path(str(paths["session_base"]) + ".session")
    if not session_file.exists() or not ENV_FILE.exists():
        status["notes"].append("using existing signal CSV; Telegram session or env file unavailable")
        return
    step = run_cmd([
        sys.executable,
        "obw_platform/telegram_signal_tools/fetch_telegram_channel_signals.py",
        "--env-file",
        str(ENV_FILE),
        "--channel",
        "https://t.me/darkknighttrade",
        "--session",
        str(paths["session_base"]),
        "--out-jsonl",
        str(paths["jsonl"]),
        "--limit",
        str(args.telegram_limit),
        "--replace",
    ], out_dir / "logs" / "fetch_telegram.log", timeout_sec=args.telegram_timeout_sec)
    status["last_step"] = step
    if step["returncode"] != 0:
        status["notes"].append("Telegram refresh failed; continuing with existing CSV if present")
        return
    step = run_cmd([
        sys.executable,
        "obw_platform/telegram_signal_tools/normalize_telegram_signal_jsonl.py",
        "--jsonl",
        str(paths["jsonl"]),
        "--out-csv",
        str(paths["csv"]),
    ], out_dir / "logs" / "normalize.log")
    status["last_step"] = step
    if step["returncode"] != 0:
        status["notes"].append("signal normalization failed")


def run_one_cycle(out_dir: Path, args: argparse.Namespace) -> Dict[str, Any]:
    paths = copy_seed_artifacts(out_dir)
    status: Dict[str, Any] = {
        "updated_at": fmt_dt(utc_now()),
        "status": "starting",
        "tmux_session": args.tmux_session,
        "signals_csv": str(paths["csv"]),
        "notes": [],
        "ttl_hours": args.ttl_hours,
        "entry_modes": args.entry_modes,
        "timeframe": args.timeframe,
        "warmup_days": args.warmup_days,
        "last_step": {},
    }
    write_status(out_dir, status)

    refresh_signals(paths, out_dir, args, status)
    rows = read_signal_rows(paths["csv"])
    if not rows:
        status.update({"status": "blocked_no_signals", "updated_at": fmt_dt(utc_now())})
        write_status(out_dir, status)
        return status

    signal_start = rows[0]["_dt"]
    signal_end = rows[-1]["_dt"]
    max_ttl = max(args.ttl_hours) if args.ttl_hours else 72
    data_end = max(utc_now(), signal_end + timedelta(hours=float(max_ttl)))
    bars = calc_bars(signal_start, data_end, args.timeframe, args.warmup_days)

    data_dir = out_dir / "data"
    universe_file = data_dir / "darkknighttrade_universe_signal_history.txt"
    universe = write_universe(rows, universe_file)
    npz_path = data_dir / ("darkknighttrade_%s_%sb_signal_window.npz" % (args.timeframe, bars))
    db_path = data_dir / ("darkknighttrade_price_indicators_%s_%sb_signal_window.sqlite" % (args.timeframe, bars))

    status.update({
        "status": "collecting_ohlcv",
        "updated_at": fmt_dt(utc_now()),
        "signal_start_utc": fmt_dt(signal_start),
        "signal_end_utc": fmt_dt(signal_end),
        "ohlcv_start_utc": fmt_dt(signal_start - timedelta(days=args.warmup_days)),
        "data_end_utc": fmt_dt(data_end),
        "bars": bars,
        "universe": universe,
        "universe_file": str(universe_file),
        "npz": str(npz_path),
        "price_db": str(db_path),
    })
    write_status(out_dir, status)

    if not universe:
        status.update({"status": "blocked_no_universe", "updated_at": fmt_dt(utc_now())})
        write_status(out_dir, status)
        return status

    step = run_cmd([
        sys.executable,
        "obw_platform/telegram_signal_tools/fetch_futures_ohlcv_npz_v1.py",
        "--exchange",
        args.exchange,
        "--universe-file",
        str(universe_file),
        "--out",
        str(npz_path),
        "--timeframe",
        args.timeframe,
        "--bars",
        str(bars),
        "--sleep-sec",
        str(args.ohlcv_sleep_sec),
        "--min-bars",
        str(args.min_bars),
        "--since-utc",
        fmt_dt(signal_start - timedelta(days=args.warmup_days)),
        "--until-utc",
        fmt_dt(data_end),
        "--max-empty",
        str(args.max_empty_batches),
    ], out_dir / "logs" / "fetch_ohlcv_npz.log", timeout_sec=args.ohlcv_timeout_sec)
    status["last_step"] = step
    status["updated_at"] = fmt_dt(utc_now())
    write_status(out_dir, status)
    if step["returncode"] != 0:
        status["status"] = "blocked_ohlcv_failed"
        write_status(out_dir, status)
        return status

    status["status"] = "building_price_db"
    write_status(out_dir, status)
    step = run_cmd([
        sys.executable,
        "telegram_standard_bt_bundle/telegram_signal_standard_bt/npz_to_price_indicators_db.py",
        "--npz",
        str(npz_path),
        "--out-db",
        str(db_path),
        "--replace",
    ], out_dir / "logs" / "npz_to_price_db.log")
    status["last_step"] = step
    status["updated_at"] = fmt_dt(utc_now())
    write_status(out_dir, status)
    if step["returncode"] != 0:
        status["status"] = "blocked_price_db_failed"
        write_status(out_dir, status)
        return status

    status["status"] = "running_v21_grid"
    write_status(out_dir, status)
    runs: List[Dict[str, Any]] = []
    for ttl in args.ttl_hours:
        for entry_mode in args.entry_modes:
            if stop_requested:
                break
            target_dir = out_dir / "grid" / ("ttl_%sh" % ttl) / entry_mode
            step = run_cmd([
                sys.executable,
                str(MODULE_DIR / "compare_channels_v21.py"),
                "--signals-csv",
                str(paths["csv"]),
                "--price-db",
                str(db_path),
                "--v21-config",
                str(V21_CONFIG),
                "--out-dir",
                str(target_dir),
                "--dca-counts",
                "0",
                "--ttl-hours",
                str(ttl),
                "--entry-mode",
                entry_mode,
                "--side",
                "both",
                "--capital-mode",
                "same_max",
                "--target-notional",
                str(args.target_notional),
            ], target_dir / "compare.log")
            runs.append({"ttl_hours": ttl, "entry_mode": entry_mode, **step})
            status["last_step"] = step
            status["grid_runs"] = runs
            status["updated_at"] = fmt_dt(utc_now())
            write_status(out_dir, status)
        if stop_requested:
            break

    status["status"] = "interrupted_after_cycle" if stop_requested else "cycle_complete"
    status["updated_at"] = fmt_dt(utc_now())
    status["grid_runs"] = runs
    write_status(out_dir, status)
    return status


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=str(REPORT_ROOT / ("darkknight_v21_signal_loop_" + utc_now().strftime("%Y%m%d"))))
    ap.add_argument("--tmux-session", default="darkknight_v21_signal_loop")
    ap.add_argument("--exchange", default="bingx")
    ap.add_argument("--timeframe", default="3m")
    ap.add_argument("--warmup-days", type=int, default=14)
    ap.add_argument("--min-bars", type=int, default=5000)
    ap.add_argument("--ohlcv-sleep-sec", type=float, default=0.12)
    ap.add_argument("--ohlcv-timeout-sec", type=float, default=0.0)
    ap.add_argument("--max-empty-batches", type=int, default=250)
    ap.add_argument("--telegram-limit", type=int, default=1500)
    ap.add_argument("--telegram-timeout-sec", type=float, default=180.0)
    ap.add_argument("--ttl-hours", type=float, nargs="+", default=[24.0, 48.0, 72.0, 96.0, 120.0, 168.0])
    ap.add_argument("--entry-modes", nargs="+", default=["first_bar", "touch_zone", "close_in_zone"])
    ap.add_argument("--target-notional", type=float, default=100.0)
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--sleep-sec", type=float, default=6 * 3600.0)
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    out_dir = Path(args.out_dir)
    while True:
        run_one_cycle(out_dir, args)
        if not args.loop or stop_requested:
            break
        time.sleep(max(1.0, args.sleep_sec))


if __name__ == "__main__":
    main()
