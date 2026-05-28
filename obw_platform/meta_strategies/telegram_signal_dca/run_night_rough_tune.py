#!/usr/bin/env python3
"""Overnight rough tune coordinator for Telegram DCA and copy-trading research.

This script is deliberately paper/backtest-only. It only scans local files and
invokes local comparison scripts when complete input pairs are present. Missing
data is recorded explicitly in the report instead of silently pretending that a
tune ran.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


ROOT = Path(__file__).resolve().parents[3]
MODULE_DIR = ROOT / "obw_platform" / "meta_strategies" / "telegram_signal_dca"
REPORT_ROOT = MODULE_DIR / "reports"
V21_CONFIG = ROOT / "obw_platform" / "configs" / "V21_strict_trend_stable_live_static9p38.yaml"

TELEGRAM_SOURCES = [
    {"name": "darkknighttrade", "dca_hint": 3, "priority": "normal"},
    {"name": "Nevskiyh", "dca_hint": 3, "priority": "normal"},
    {"name": "topslivs", "dca_hint": 2, "priority": "normal"},
    {"name": "Treyding_Signaly_Kripto", "dca_hint": 3, "priority": "low"},
]

BINANCE_LEADS = [
    "4728671486012660992",
    "4751838302089254401",
    "4906010685108267264",
]

TTLS = [24, 48, 72, 96]
DCA_COUNTS = "0,1,2,3"
SLIPPAGE_BPS = [9.38, 18.7]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def safe_name(raw: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in raw)[:80]


def has_price_indicators(path: Path) -> bool:
    try:
        con = sqlite3.connect(str(path))
        try:
            rows = con.execute(
                "select name from sqlite_master where type='table' and name='price_indicators'"
            ).fetchall()
        finally:
            con.close()
        return bool(rows)
    except Exception:
        return False


def find_files(patterns: Iterable[str]) -> List[Path]:
    out: List[Path] = []
    for pattern in patterns:
        out.extend(ROOT.glob(pattern))
    seen: Dict[str, Path] = {}
    for path in out:
        if path.exists() and path.is_file():
            seen[str(path)] = path
    return [seen[k] for k in sorted(seen)]


def source_tokens(source: str) -> List[str]:
    low = source.lower()
    tokens = {low, low.replace("_", ""), low.replace("_", "-"), low.replace("-", "_")}
    if low == "treyding_signaly_kripto":
        tokens.update({"treyding", "signaly", "kripto"})
    return sorted(tokens)


def choose_telegram_inputs(source: str) -> Tuple[Optional[Path], Optional[Path], List[str]]:
    tokens = source_tokens(source)
    csvs = find_files([
        "telegram_standard_bt_bundle/**/*.csv",
        "runs/**/*.csv",
        "obw_platform/meta_strategies/telegram_signal_dca/**/*.csv",
    ])
    dbs = find_files([
        "telegram_standard_bt_bundle/**/*.db",
        "telegram_standard_bt_bundle/**/*.sqlite",
        "runs/**/*.db",
        "runs/**/*.sqlite",
        "obw_platform/**/*.db",
        "obw_platform/**/*.sqlite",
    ])
    csv_candidates = [
        p for p in csvs
        if "signal" in p.name.lower() and any(tok in str(p).lower() for tok in tokens)
    ]
    db_candidates = [
        p for p in dbs
        if any(tok in str(p).lower() for tok in tokens) and has_price_indicators(p)
    ]
    notes: List[str] = []
    if not csv_candidates:
        notes.append("missing source-specific signals CSV")
    if not db_candidates:
        notes.append("missing source-specific SQLite price_indicators DB")
    return (
        csv_candidates[-1] if csv_candidates else None,
        db_candidates[-1] if db_candidates else None,
        notes,
    )


def choose_binance_positions(lead: str) -> Tuple[Optional[Path], List[str]]:
    paths = find_files([
        "obw_platform/meta_strategies/telegram_signal_dca/reports/**/*.csv",
        "obw_platform/meta_strategies/binance_online_copytrading/reports/**/*.csv",
    ])
    candidates = [
        p for p in paths
        if lead in str(p) and ("position" in p.name.lower() or "history" in p.name.lower())
    ]
    if candidates:
        return candidates[-1], []
    return None, ["missing normalized Binance copy position history CSV"]


def run_cmd(cmd: List[str], log_path: Path) -> Dict[str, Any]:
    started = utc_now()
    with log_path.open("a", encoding="utf-8") as log:
        log.write("\n$ %s\n" % " ".join(cmd))
        log.flush()
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        rc = proc.wait()
    return {"cmd": cmd, "started_at": started, "finished_at": utc_now(), "returncode": rc, "log": str(log_path)}


def one_scan(out_dir: Path, iteration: int) -> Dict[str, Any]:
    scan_dir = out_dir / ("scan_%03d" % iteration)
    scan_dir.mkdir(parents=True, exist_ok=True)
    results: Dict[str, Any] = {
        "iteration": iteration,
        "started_at": utc_now(),
        "ttl_hours": TTLS,
        "dca_counts": DCA_COUNTS,
        "slippage_bps": SLIPPAGE_BPS,
        "telegram": [],
        "binance_copy": [],
    }

    for source in TELEGRAM_SOURCES:
        sig_csv, price_db, notes = choose_telegram_inputs(source["name"])
        item: Dict[str, Any] = {
            "source": source["name"],
            "dca_hint": source["dca_hint"],
            "priority": source["priority"],
            "signals_csv": str(sig_csv) if sig_csv else "",
            "price_db": str(price_db) if price_db else "",
            "runs": [],
            "notes": notes,
        }
        if sig_csv and price_db:
            for ttl in TTLS:
                target_dir = scan_dir / "telegram" / safe_name(source["name"]) / ("ttl_%sh" % ttl)
                log_path = target_dir / "compare.log"
                target_dir.mkdir(parents=True, exist_ok=True)
                cmd = [
                    "python",
                    str(MODULE_DIR / "compare_channels_v21.py"),
                    "--signals-csv",
                    str(sig_csv),
                    "--price-db",
                    str(price_db),
                    "--v21-config",
                    str(V21_CONFIG),
                    "--out-dir",
                    str(target_dir),
                    "--dca-counts",
                    DCA_COUNTS,
                    "--ttl-hours",
                    str(ttl),
                    "--entry-mode",
                    "first_bar",
                    "--capital-mode",
                    "same_max",
                    "--target-notional",
                    "100",
                ]
                item["runs"].append(run_cmd(cmd, log_path))
            item["notes"].append("stress slippage 18.7bp requires config-level support; recorded as pending")
        results["telegram"].append(item)

    for lead in BINANCE_LEADS:
        positions_csv, notes = choose_binance_positions(lead)
        item = {
            "lead_id": lead,
            "positions_csv": str(positions_csv) if positions_csv else "",
            "runs": [],
            "notes": notes,
        }
        if positions_csv:
            target_dir = scan_dir / "binance_copy" / lead
            log_path = target_dir / "compare.log"
            target_dir.mkdir(parents=True, exist_ok=True)
            cmd = [
                "python",
                str(MODULE_DIR / "compare_binance_copy_positions_dca.py"),
                "--positions-csv",
                str(positions_csv),
                "--v21-config",
                str(V21_CONFIG),
                "--out-dir",
                str(target_dir),
                "--dca-counts",
                DCA_COUNTS,
                "--target-notional",
                "100",
            ]
            item["runs"].append(run_cmd(cmd, log_path))
            item["notes"].append("stress slippage 18.7bp requires config-level support; recorded as pending")
        results["binance_copy"].append(item)

    results["finished_at"] = utc_now()
    (scan_dir / "manifest.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    write_report(out_dir, results)
    return results


def write_report(out_dir: Path, latest: Dict[str, Any]) -> None:
    lines: List[str] = [
        "# Night Rough Tune",
        "",
        "Paper/backtest only. No live orders. No secrets are read or printed.",
        "",
        "- Latest scan: `%s`" % latest["finished_at"],
        "- TTL grid: `%s`" % ", ".join(str(x) for x in TTLS),
        "- DCA grid: `%s`" % DCA_COUNTS,
        "- Slippage grid requested: `%s` bp" % ", ".join(str(x) for x in SLIPPAGE_BPS),
        "",
        "## Telegram Sources",
        "",
        "| source | status | signals | price DB | notes |",
        "|---|---|---|---|---|",
    ]
    for item in latest["telegram"]:
        status = "ran %d jobs" % len(item["runs"]) if item["runs"] else "blocked"
        lines.append(
            "| %s | %s | `%s` | `%s` | %s |"
            % (
                item["source"],
                status,
                item["signals_csv"] or "",
                item["price_db"] or "",
                "; ".join(item["notes"]) or "",
            )
        )
    lines.extend(["", "## Binance Copy Leads", "", "| lead | status | positions CSV | notes |", "|---|---|---|---|"])
    for item in latest["binance_copy"]:
        status = "ran %d jobs" % len(item["runs"]) if item["runs"] else "blocked"
        lines.append(
            "| %s | %s | `%s` | %s |"
            % (item["lead_id"], status, item["positions_csv"] or "", "; ".join(item["notes"]) or "")
        )
    lines.extend([
        "",
        "## Ranking",
        "",
        "Ranking by max-cap normalized return, PF, maxDD, and trade count will be generated after source-specific input data is present.",
    ])
    (out_dir / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="")
    ap.add_argument("--watch-hours", type=float, default=0.0)
    ap.add_argument("--sleep-sec", type=float, default=1800.0)
    args = ap.parse_args()

    if args.out_dir:
        out_dir = Path(args.out_dir)
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        out_dir = REPORT_ROOT / ("night_tune_%s" % stamp)
    out_dir.mkdir(parents=True, exist_ok=True)

    deadline = time.time() + max(0.0, args.watch_hours) * 3600.0
    iteration = 1
    while True:
        one_scan(out_dir, iteration)
        if args.watch_hours <= 0 or time.time() >= deadline:
            break
        iteration += 1
        time.sleep(max(1.0, args.sleep_sec))


if __name__ == "__main__":
    main()
