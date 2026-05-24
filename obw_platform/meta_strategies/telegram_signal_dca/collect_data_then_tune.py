#!/usr/bin/env python3
"""Collect missing Telegram/Binance research inputs, then run rough tune.

Paper/backtest-only. It does not place orders and does not print secrets. The
script is meant to run in tmux: collect as much data as possible, then launch
`run_night_rough_tune.py` automatically. If interrupted, it still runs the tuner
against whatever inputs were collected before exiting.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


ROOT = Path(__file__).resolve().parents[3]
MODULE_DIR = ROOT / "obw_platform" / "meta_strategies" / "telegram_signal_dca"
REPORT_ROOT = MODULE_DIR / "reports"
ENV_FILE = ROOT / ".env"
TELEGRAM_SESSION_SRC = ROOT / "runs" / "telegram_paper" / "darkknight_session.session"

TELEGRAM_CHANNELS = [
    ("darkknighttrade", "https://t.me/darkknighttrade"),
    ("Nevskiyh", "https://t.me/Nevskiyh"),
    ("topslivs", "https://t.me/topslivs"),
    ("Treyding_Signaly_Kripto", "https://t.me/Treyding_Signaly_Kripto"),
]

BINANCE_LEADS = [
    "4728671486012660992",
    "4751838302089254401",
    "4906010685108267264",
]


interrupted = False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def safe_name(raw: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in raw)[:80]


def on_signal(signum: int, _frame: Any) -> None:
    global interrupted
    interrupted = True
    print("[interrupt] received signal %s; will run tuner on collected data" % signum, flush=True)


def run_cmd(cmd: List[str], log_path: Path, *, timeout_sec: float = 0.0) -> Dict[str, Any]:
    started = utc_now()
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
        "finished_at": utc_now(),
        "returncode": rc,
        "timed_out": timed_out,
        "log": str(log_path),
    }


def read_symbols_from_csv(path: Path) -> List[str]:
    out: List[str] = []
    seen = set()
    if not path.exists() or path.stat().st_size == 0:
        return out
    with path.open("r", encoding="utf-8", newline="") as fp:
        for row in csv.DictReader(fp):
            sym = str(row.get("symbol") or "").strip().upper()
            if not sym:
                continue
            if "/" in sym:
                base = sym.split("/", 1)[0]
            elif sym.endswith("USDT") and len(sym) > 4:
                base = sym[:-4]
            else:
                base = sym
            if base and base not in seen:
                seen.add(base)
                out.append(base)
    return out


def ensure_session(dst_no_suffix: Path) -> None:
    dst = Path(str(dst_no_suffix) + ".session")
    if dst.exists():
        return
    if TELEGRAM_SESSION_SRC.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(TELEGRAM_SESSION_SRC), str(dst))


def collect_telegram_channel(name: str, channel: str, out_dir: Path, args: argparse.Namespace) -> Dict[str, Any]:
    channel_dir = out_dir / "telegram" / safe_name(name)
    channel_dir.mkdir(parents=True, exist_ok=True)
    session = channel_dir / ("%s_session" % safe_name(name))
    jsonl = channel_dir / ("%s_signals.jsonl" % safe_name(name))
    csv_path = channel_dir / ("%s_signals.csv" % safe_name(name))
    universe = channel_dir / ("%s_universe.txt" % safe_name(name))
    npz = channel_dir / ("%s_%s_%sb.npz" % (safe_name(name), args.timeframe, args.bars))
    db = channel_dir / ("%s_price_indicators_%s_%sb.sqlite" % (safe_name(name), args.timeframe, args.bars))

    ensure_session(session)
    item: Dict[str, Any] = {
        "source": name,
        "channel": channel,
        "jsonl": str(jsonl),
        "signals_csv": str(csv_path),
        "universe_file": str(universe),
        "npz": str(npz),
        "price_db": str(db),
        "steps": [],
        "symbols": [],
        "notes": [],
    }

    if not Path(str(session) + ".session").exists():
        item["notes"].append("missing authorized Telethon session")
        return item

    item["steps"].append(run_cmd([
        "python",
        "obw_platform/telegram_signal_tools/fetch_telegram_channel_signals.py",
        "--env-file",
        str(ENV_FILE),
        "--channel",
        channel,
        "--session",
        str(session),
        "--out-jsonl",
        str(jsonl),
        "--limit",
        str(args.telegram_limit),
        "--replace",
    ], channel_dir / "fetch.log", timeout_sec=args.telegram_timeout_sec))

    if item["steps"][-1]["returncode"] != 0:
        item["notes"].append("telegram fetch failed")
        return item

    item["steps"].append(run_cmd([
        "python",
        "obw_platform/telegram_signal_tools/normalize_telegram_signal_jsonl.py",
        "--jsonl",
        str(jsonl),
        "--out-csv",
        str(csv_path),
    ], channel_dir / "normalize.log"))

    symbols = read_symbols_from_csv(csv_path)
    item["symbols"] = symbols
    if not symbols:
        item["notes"].append("no parsed signals/symbols")
        return item

    universe.write_text("\n".join(symbols) + "\n", encoding="utf-8")
    item["steps"].append(run_cmd([
        "python",
        "obw_platform/telegram_signal_tools/fetch_futures_ohlcv_npz_v1.py",
        "--exchange",
        args.exchange,
        "--universe-file",
        str(universe),
        "--out",
        str(npz),
        "--timeframe",
        args.timeframe,
        "--bars",
        str(args.bars),
        "--sleep-sec",
        str(args.ohlcv_sleep_sec),
        "--min-bars",
        str(args.min_bars),
    ], channel_dir / "fetch_ohlcv.log", timeout_sec=args.ohlcv_timeout_sec))

    if item["steps"][-1]["returncode"] != 0:
        item["notes"].append("OHLCV fetch failed")
        return item

    item["steps"].append(run_cmd([
        "python",
        "telegram_standard_bt_bundle/telegram_signal_standard_bt/npz_to_price_indicators_db.py",
        "--npz",
        str(npz),
        "--out-db",
        str(db),
        "--replace",
    ], channel_dir / "npz_to_db.log"))
    return item


def collect_binance_lead(lead_id: str, out_dir: Path, args: argparse.Namespace) -> Dict[str, Any]:
    lead_dir = out_dir / "binance_copy" / lead_id
    lead_dir.mkdir(parents=True, exist_ok=True)
    item: Dict[str, Any] = {
        "lead_id": lead_id,
        "positions_csv": str(lead_dir / "position_history_normalized.csv"),
        "out_dir": str(lead_dir),
        "steps": [],
        "notes": [],
    }
    item["steps"].append(run_cmd([
        "python",
        "obw_platform/meta_strategies/telegram_signal_dca/binance_copy_contrarian_on_close.py",
        "--portfolio-id",
        lead_id,
        "--time-range",
        args.binance_time_range,
        "--page-size",
        str(args.binance_page_size),
        "--max-pages",
        str(args.binance_max_pages),
        "--out-dir",
        str(lead_dir),
        "--ttl-hours",
        "72",
        "--target-notional",
        "100",
        "--dca-counts",
        "0,1,2,3",
        "--exit-on-reversal",
        "--sleep-sec",
        str(args.binance_sleep_sec),
    ], lead_dir / "collect_and_compare.log", timeout_sec=args.binance_timeout_sec))
    if item["steps"][-1]["returncode"] != 0:
        item["notes"].append("binance collection/comparison failed")
    return item


def write_status(out_dir: Path, status: Dict[str, Any]) -> None:
    (out_dir / "collect_then_tune_manifest.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    lines = [
        "# Collect Then Tune",
        "",
        "- Started: `%s`" % status.get("started_at", ""),
        "- Updated: `%s`" % utc_now(),
        "- Interrupted: `%s`" % status.get("interrupted", False),
        "",
        "## Telegram",
        "",
        "| source | symbols | notes |",
        "|---|---:|---|",
    ]
    for item in status.get("telegram", []):
        lines.append("| %s | %s | %s |" % (
            item.get("source", ""),
            len(item.get("symbols") or []),
            "; ".join(item.get("notes") or []),
        ))
    lines.extend(["", "## Binance", "", "| lead | notes |", "|---|---|"])
    for item in status.get("binance", []):
        lines.append("| %s | %s |" % (item.get("lead_id", ""), "; ".join(item.get("notes") or [])))
    lines.extend(["", "## Tune", "", "`%s`" % status.get("tune_report", "")])
    (out_dir / "COLLECT_THEN_TUNE.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_tuner(out_dir: Path, args: argparse.Namespace) -> Dict[str, Any]:
    return run_cmd([
        "python",
        "obw_platform/meta_strategies/telegram_signal_dca/run_night_rough_tune.py",
        "--out-dir",
        str(out_dir),
        "--watch-hours",
        str(args.tune_watch_hours),
        "--sleep-sec",
        str(args.tune_sleep_sec),
    ], out_dir / "run_night_rough_tune.log")


def main() -> None:
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="")
    ap.add_argument("--telegram-limit", type=int, default=1000)
    ap.add_argument("--telegram-timeout-sec", type=float, default=600)
    ap.add_argument("--exchange", default="bingx")
    ap.add_argument("--timeframe", default="3m")
    ap.add_argument("--bars", type=int, default=7200)
    ap.add_argument("--min-bars", type=int, default=500)
    ap.add_argument("--ohlcv-sleep-sec", type=float, default=0.25)
    ap.add_argument("--ohlcv-timeout-sec", type=float, default=3600)
    ap.add_argument("--binance-time-range", default="90D")
    ap.add_argument("--binance-page-size", type=int, default=50)
    ap.add_argument("--binance-max-pages", type=int, default=5)
    ap.add_argument("--binance-sleep-sec", type=float, default=0.08)
    ap.add_argument("--binance-timeout-sec", type=float, default=3600)
    ap.add_argument("--tune-watch-hours", type=float, default=0.0)
    ap.add_argument("--tune-sleep-sec", type=float, default=1800)
    args = ap.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir) if args.out_dir else REPORT_ROOT / ("night_tune_%s_collect" % stamp)
    out_dir.mkdir(parents=True, exist_ok=True)

    status: Dict[str, Any] = {
        "started_at": utc_now(),
        "out_dir": str(out_dir),
        "telegram": [],
        "binance": [],
        "interrupted": False,
        "tune": None,
        "tune_report": str(out_dir / "REPORT.md"),
    }
    write_status(out_dir, status)

    try:
        for name, channel in TELEGRAM_CHANNELS:
            if interrupted:
                break
            item = collect_telegram_channel(name, channel, out_dir, args)
            status["telegram"].append(item)
            write_status(out_dir, status)

        for lead_id in BINANCE_LEADS:
            if interrupted:
                break
            item = collect_binance_lead(lead_id, out_dir, args)
            status["binance"].append(item)
            write_status(out_dir, status)
    finally:
        status["interrupted"] = interrupted
        status["tune"] = run_tuner(out_dir, args)
        status["finished_at"] = utc_now()
        write_status(out_dir, status)


if __name__ == "__main__":
    main()
