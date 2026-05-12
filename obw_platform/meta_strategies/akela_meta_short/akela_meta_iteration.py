#!/usr/bin/env python3
"""Run one Akela meta-short research iteration.

This script is deliberately conservative: it orchestrates existing research
tools and summarizes evidence. It does not change exchange/backtest math.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[3]
LANE_DIR = ROOT / "obw_platform" / "meta_strategies" / "akela_meta_short"
RAW_REPORT_ROOT = ROOT / "_reports" / "akela_meta_short"
SUMMARY_DIR = LANE_DIR / "reports"
DATA_DIR = LANE_DIR / "data"
DATA_STATE_PATH = RAW_REPORT_ROOT / "data_collection_state.json"

PRIMARY_NPZ = ROOT / "DB" / "akela_top200_1m_30d.research_v2_2_no_cross.npz"
SHORTLIST_NPZ = ROOT / "DB" / "fast_cache_akela_shortlist_1m_30d.npz"
FIVE_MIN_NPZ = ROOT / "DB" / "combined_cache_5m_5000_04.09.phase0_top100.research_v2_2_no_cross.npz"
AKELA_TOP200_DB = ROOT / "DB" / "akela_top200_1m_30d.db"

YEARLY_CANDIDATES = [
    "IDOL",
    "FREEDOMMONEY",
    "MAXXING",
    "SUP",
]

KNOWN_YEARLY_NPZ = {
    "FREEDOMMONEY": ROOT / "DB" / "fast_cache_1m_freedommoney_1y_bingx.npz",
    "MAXXING": ROOT / "DB" / "fast_cache_1m_maxxing_1y_bingx.npz",
}

PROFILES = [
    {
        "name": "baseline",
        "ranker_args": [],
        "short_args": [],
    },
    {
        "name": "sensitive_failed_pump",
        "ranker_args": [
            "--pump-pct",
            "0.03",
            "--setup-min-up-pct",
            "0.008",
            "--setup-min-down-pct",
            "0.008",
            "--late-dd-pct",
            "45",
        ],
        "short_args": [
            "--pump-pct",
            "0.07",
            "--pump-window",
            "1440",
            "--pump-fail-window",
            "480",
            "--high-lookback",
            "7200",
        ],
    },
    {
        "name": "strict_late_decay",
        "ranker_args": [
            "--pump-pct",
            "0.06",
            "--setup-min-up-pct",
            "0.015",
            "--setup-min-down-pct",
            "0.012",
            "--phase-recent-window",
            "480",
            "--late-dd-pct",
            "65",
        ],
        "short_args": [
            "--pump-pct",
            "0.12",
            "--pump-window",
            "1440",
            "--pump-fail-window",
            "480",
            "--high-lookback",
            "7200",
        ],
    },
]


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def run_cmd(name: str, cmd: list[str], cwd: Path, log_path: Path, timeout: int) -> dict:
    started = datetime.now(timezone.utc)
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    log_path.write_text(proc.stdout, encoding="utf-8")
    ended = datetime.now(timezone.utc)
    return {
        "name": name,
        "returncode": proc.returncode,
        "seconds": round((ended - started).total_seconds(), 2),
        "cmd": cmd,
        "log": str(log_path.relative_to(ROOT)),
    }


def load_data_state() -> dict:
    if not DATA_STATE_PATH.exists():
        return {}
    try:
        return json.loads(DATA_STATE_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def save_data_state(state: dict) -> None:
    DATA_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def candidate_yearly_npz(symbol: str) -> Path:
    if symbol in KNOWN_YEARLY_NPZ:
        return KNOWN_YEARLY_NPZ[symbol]
    return ROOT / "DB" / f"akela_meta_short_1m_1y_{symbol.lower()}_bingx.npz"


def npz_bar_count(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        import numpy as np

        with np.load(path, allow_pickle=True) as z:
            if "timestamp_s" in z:
                return int(len(z["timestamp_s"]))
            if "close" in z:
                return int(len(z["close"]))
    except Exception:
        return 0
    return 0


def recent_failed_fetch(symbol: str, data_state: dict, max_age_hours: float = 6.0) -> bool:
    item = data_state.get(symbol, {})
    if item.get("last_status") != "failed":
        return False
    ts = item.get("last_attempt_utc")
    if not ts:
        return False
    try:
        last = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return False
    age = datetime.now(timezone.utc) - last
    return age.total_seconds() < max_age_hours * 3600


def build_yearly_data_jobs(run_dir: Path) -> tuple[list[tuple[str, list[str], int]], list[dict]]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data_state = load_data_state()
    jobs: list[tuple[str, list[str], int]] = []
    plan: list[dict] = []
    for symbol in YEARLY_CANDIDATES:
        target = candidate_yearly_npz(symbol)
        bars = npz_bar_count(target)
        item = {
            "symbol": symbol,
            "target_npz": str(target.relative_to(ROOT)),
            "existing_bars": bars,
            "status": "present" if bars >= 100000 else "missing",
            "action": "skip",
        }
        if bars >= 100000:
            plan.append(item)
            continue
        if recent_failed_fetch(symbol, data_state):
            item["status"] = "recent_fetch_failed"
            item["action"] = "skip_until_failure_cools_down"
            plan.append(item)
            continue
        universe_path = DATA_DIR / f"universe_{symbol.lower()}_1m_1y.txt"
        universe_path.write_text(symbol + "\n", encoding="utf-8")
        item["universe_file"] = str(universe_path.relative_to(ROOT))
        item["action"] = "fetch_1m_1y_bingx"
        plan.append(item)
        jobs.append(
            (
                f"fetch_1y_data:{symbol.lower()}",
                [
                    "python3",
                    "obw_platform/scripts/fetch_backfill_ohlcv_npz_from_now_v1.py",
                    "--input-csv",
                    str(universe_path),
                    "--timeframe",
                    "1m",
                    "--back-bars",
                    "525600",
                    "--exchange",
                    "bingx",
                    "--ccxt-symbol-format",
                    "usdtm",
                    "--limit",
                    "1000",
                    "--sleep-sec",
                    "0.2",
                    "--npz-out",
                    str(target),
                    "--feature-set",
                    "none",
                    "--cache-pack-trend",
                ],
                7200,
            )
        )
    (run_dir / "yearly_data_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return jobs, plan


def update_data_state_from_results(results: list[dict]) -> None:
    data_state = load_data_state()
    changed = False
    for result in results:
        name = str(result.get("name", ""))
        if not name.startswith("fetch_1y_data:"):
            continue
        symbol = name.split(":", 1)[1].upper()
        target = candidate_yearly_npz(symbol)
        status = "succeeded" if result.get("returncode") == 0 and npz_bar_count(target) > 0 else "failed"
        data_state[symbol] = {
            "last_attempt_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "last_status": status,
            "target_npz": str(target.relative_to(ROOT)),
            "bars": npz_bar_count(target),
            "log": result.get("log"),
        }
        changed = True
    if changed:
        save_data_state(data_state)


def read_csv_rows(path: Path, limit: int = 10) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        return [row for _, row in zip(range(limit), reader)]


def first_existing(paths: Iterable[Path]) -> Path | None:
    for path in paths:
        if path.exists():
            return path
    return None


def npz_has_btc(path: Path) -> bool:
    try:
        import numpy as np

        with np.load(path, allow_pickle=True) as z:
            symbols = []
            for key in ("symbols", "symbol", "pairs"):
                if key in z:
                    symbols = [str(x) for x in z[key].tolist()]
                    break
            if not symbols:
                symbols = [key[:-6] for key in z.keys() if key.endswith("_close")]
            return any(sym.startswith("BTC/") or sym.startswith("BTCUSDT") for sym in symbols)
    except Exception:
        return False


def first_existing_with_btc(paths: Iterable[Path]) -> Path | None:
    for path in paths:
        if path.exists() and npz_has_btc(path):
            return path
    return None


def symbol_from_row(row: dict[str, str]) -> str | None:
    for key in ("symbol", "pair", "market", "sym"):
        val = row.get(key)
        if val:
            return val
    return None


def collect_mentions(rows_by_report: dict[str, list[dict[str, str]]]) -> dict[str, list[str]]:
    mentions: dict[str, list[str]] = {}
    for report_name, rows in rows_by_report.items():
        for row in rows:
            symbol = symbol_from_row(row)
            if not symbol:
                continue
            mentions.setdefault(symbol, []).append(report_name)
    return {k: v for k, v in sorted(mentions.items(), key=lambda item: (-len(item[1]), item[0]))}


def format_table(rows: list[dict[str, str]], keys: list[str], limit: int = 10) -> str:
    if not rows:
        return "_No rows produced._"
    lines = ["| " + " | ".join(keys) + " |", "| " + " | ".join(["---"] * len(keys)) + " |"]
    for row in rows[:limit]:
        lines.append("| " + " | ".join((row.get(k, "") or "")[:80] for k in keys) + " |")
    return "\n".join(lines)


def main() -> int:
    stamp = utc_stamp()
    RAW_REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
    run_dir = RAW_REPORT_ROOT / stamp
    latest_dir = RAW_REPORT_ROOT / "latest"
    run_dir.mkdir(parents=True, exist_ok=True)

    phase_npz = first_existing_with_btc([SHORTLIST_NPZ, FIVE_MIN_NPZ, PRIMARY_NPZ])
    if phase_npz is None:
        phase_npz = first_existing([SHORTLIST_NPZ, PRIMARY_NPZ, FIVE_MIN_NPZ])
    short_db = AKELA_TOP200_DB if AKELA_TOP200_DB.exists() else None
    if phase_npz is None and short_db is None:
        print("No Akela NPZ dataset found.", file=sys.stderr)
        return 2

    jobs = []
    data_jobs, yearly_data_plan = build_yearly_data_jobs(run_dir)
    jobs.extend(data_jobs)
    report_paths: dict[str, dict[str, Path]] = {}
    for profile in PROFILES:
        profile_name = profile["name"]
        phase_csv = run_dir / f"phase_rank_{profile_name}.csv"
        phase_json = run_dir / f"phase_rank_{profile_name}.json"
        monthly_detail = run_dir / f"monthly_detail_{profile_name}.csv"
        monthly_summary = run_dir / f"monthly_summary_{profile_name}.csv"
        monthly_json = run_dir / f"monthly_{profile_name}.json"
        short_csv = run_dir / f"short_leg_rank_{profile_name}.csv"
        short_json = run_dir / f"short_leg_rank_{profile_name}.json"

        report_paths[profile_name] = {
            "phase": phase_csv,
            "monthly": monthly_summary,
            "short_leg": short_csv,
        }

        jobs.extend(
            [
                (
                    f"phase_proxy_rank:{profile_name}",
                    [
                        "python3",
                        "obw_platform/rank_fast_cache_akela_phase_proxybt.py",
                        "--npz",
                        str(phase_npz),
                        "--out",
                        str(phase_csv),
                        "--json-out",
                        str(phase_json),
                        "--top",
                        "80",
                        "--min-bars",
                        "1000",
                    ]
                    + profile["ranker_args"],
                    1800,
                ),
                (
                    f"monthly_rolling_phase_proxy:{profile_name}",
                    [
                        "python3",
                        "obw_platform/monthly_akela_phase_proxybt.py",
                        "--ranker-path",
                        "obw_platform/rank_fast_cache_akela_phase_proxybt.py",
                        "--npz",
                        str(phase_npz),
                        "--mode",
                        "rolling",
                        "--rolling-days",
                        "14",
                        "--rolling-step-days",
                        "7",
                        "--detail-out",
                        str(monthly_detail),
                        "--summary-out",
                        str(monthly_summary),
                        "--json-out",
                        str(monthly_json),
                        "--top-per-period",
                        "10",
                        "--min-bars",
                        "1000",
                    ]
                    + profile["ranker_args"],
                    2400,
                ),
                (
                    f"short_leg_rank_no_backtest:{profile_name}",
                    [
                        "python3",
                        "obw_platform/rank_short_leg_all_symbols_akela_v2.py",
                        "--db",
                        str(short_db),
                        "--prefer",
                        "db",
                        "--no-backtest",
                        "--top",
                        "120",
                        "--out",
                        str(short_csv),
                        "--json-out",
                        str(short_json),
                        "--min-bars",
                        "1000",
                    ]
                    + profile["short_args"],
                    1800,
                ),
            ]
        )

    results = []
    for name, cmd, timeout in jobs:
        try:
            results.append(run_cmd(name, cmd, ROOT, run_dir / f"{name}.log", timeout))
        except subprocess.TimeoutExpired as exc:
            log_path = run_dir / f"{name}.log"
            output = exc.stdout or ""
            log_path.write_text(str(output), encoding="utf-8")
            results.append(
                {
                    "name": name,
                    "returncode": "timeout",
                    "seconds": timeout,
                    "cmd": cmd,
                    "log": str(log_path.relative_to(ROOT)),
                }
            )
    update_data_state_from_results(results)

    rows_by_report: dict[str, list[dict[str, str]]] = {}
    for profile_name, paths in report_paths.items():
        for report_name, path in paths.items():
            rows_by_report[f"{report_name}:{profile_name}"] = read_csv_rows(path, 20)
    mentions = collect_mentions(rows_by_report)
    repeated = {k: v for k, v in mentions.items() if len(v) >= 3}

    manifest = {
        "stamp": stamp,
        "phase_dataset": str(phase_npz.relative_to(ROOT)) if phase_npz else "",
        "short_dataset": str(short_db.relative_to(ROOT)) if short_db else "",
        "raw_report_dir": str(run_dir.relative_to(ROOT)),
        "results": results,
        "yearly_data_plan": yearly_data_plan,
        "profiles": [profile["name"] for profile in PROFILES],
        "repeated_candidates_min_3_reports": repeated,
    }
    (SUMMARY_DIR / "latest_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    phase_keys = ["symbol", "final_phase_short_score", "proxy_return_total_pct", "proxy_mdd_mtm_pct", "ret_total_pct"]
    monthly_keys = ["symbol", "portfolio_score", "months_tested", "positive_rate", "median_proxy_return_total_pct"]
    short_keys = ["symbol", "final_short_score", "rel_total_pct", "ret_total_pct", "market_total_pct"]

    summary = [
        "# Akela Meta Short Latest Summary",
        "",
        f"Updated: {stamp}",
        f"Phase dataset: `{phase_npz.relative_to(ROOT) if phase_npz else ''}`",
        f"Short-leg dataset: `{short_db.relative_to(ROOT) if short_db else ''}`",
        f"Raw artifacts: `{run_dir.relative_to(ROOT)}`",
        "",
        "## Job Results",
        "",
        "| job | returncode | seconds | log |",
        "| --- | --- | ---: | --- |",
    ]
    for result in results:
        summary.append(
            f"| {result['name']} | {result['returncode']} | {result['seconds']} | `{result['log']}` |"
        )
    summary.extend(
        [
            "",
            "## Repeated Candidates Across Profiles",
            "",
        ]
    )
    if repeated:
        for symbol, reports in list(repeated.items())[:20]:
            summary.append(f"- `{symbol}` appears in {len(reports)} reports: {', '.join(reports)}")
    else:
        summary.append("_No repeated top candidates across reports yet._")

    summary.extend(
        [
            "",
            "## Yearly Data Plan",
            "",
        ]
    )
    for item in yearly_data_plan:
        summary.append(
            f"- `{item['symbol']}`: {item['status']}, bars={item['existing_bars']}, action={item['action']}, target=`{item['target_npz']}`"
        )

    summary.extend(
        [
            "",
            "## Phase Proxy Top Rows",
            "",
            format_table(rows_by_report.get("phase:baseline", []), phase_keys),
            "",
            "## Monthly Stability Top Rows",
            "",
            format_table(rows_by_report.get("monthly:baseline", []), monthly_keys),
            "",
            "## Short Leg Rank Top Rows",
            "",
            format_table(rows_by_report.get("short_leg:baseline", []), short_keys),
            "",
            "## Next Research Action",
            "",
            "Investigate repeated candidates first. If repeated candidates remain empty, loosen only selector diagnostics, not backtest math.",
        ]
    )

    summary_text = "\n".join(summary) + "\n"
    (SUMMARY_DIR / "latest_summary.md").write_text(summary_text, encoding="utf-8")
    (SUMMARY_DIR / f"summary_{stamp}.md").write_text(summary_text, encoding="utf-8")

    if latest_dir.exists() or latest_dir.is_symlink():
        if latest_dir.is_symlink() or latest_dir.is_file():
            latest_dir.unlink()
        else:
            shutil.rmtree(latest_dir)
    shutil.copytree(run_dir, latest_dir)

    print(summary_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
