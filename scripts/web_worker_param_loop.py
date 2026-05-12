#!/usr/bin/env python3
"""Experimental top_1 web-worker parameter/backtest loop.

This is a local-only bridge for using ChatGPT web workers to generate and
critique parameter guesses while the local orchestrator validates candidates
with bounded backtests. It must not deploy or touch live trading.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKERS_AUTOMATE = ROOT.parent / "chrome" / "workers_automate"
sys.path.insert(0, str(WORKERS_AUTOMATE))

from chatgpt_worker_manager import (  # noqa: E402
    ChatGPTWorkerManager,
    WorkerStatus,
    acquire_nav_lock,
    human_sleep,
    release_nav_lock,
)


WORKERS = [
    {
        "id": "worker_11",
        "name": "Parameter Hypothesis Scout",
        "url": "https://chatgpt.com/g/g-p-6a002feff37c8191b4eeccadcae7da9d/c/6a02e7eb-5154-8333-93b0-edda291305f9",
        "role": "Suggest high-upside parameter guesses for fresh symbols and current champion configs.",
    },
    {
        "id": "worker_12",
        "name": "Parameter Critic and Robustness Auditor",
        "url": "https://chatgpt.com/g/g-p-6a002feff37c8191b4eeccadcae7da9d/c/6a02e7f0-0200-832d-98ce-71d00635d0a2",
        "role": "Critique parameter guesses for robustness, overfit risk, and test priority.",
    },
]

RUNTIME_DIR = Path(os.environ.get(
    "TOP1_WEB_WORKER_RUNTIME_DIR",
    str(ROOT / "continuity" / "web_worker_param_loop" / "runtime"),
)).resolve()
LOG_FILE = RUNTIME_DIR / "web_worker_param_loop.log"
STATE_FILE = RUNTIME_DIR / "web_worker_param_loop_state.json"
TREE_FILE = RUNTIME_DIR / "ui_data" / "web_worker_param_tree.json"
ARCHIVE_FILE = RUNTIME_DIR / "top1_web_worker_context.zip"
ARCHIVE_MANIFEST = RUNTIME_DIR / "top1_web_worker_context.manifest.json"
WAKEUP_FLAG = RUNTIME_DIR / "web_worker_param_wakeup.flag"
CONTRACT_FILE = ROOT / "continuity" / "web_worker_param_loop" / "ORCHESTRATOR_PROMPT.md"
BACKTEST_DIR = RUNTIME_DIR / "backtests"
BACKTEST_SUMMARY_FILE = BACKTEST_DIR / "backtest_summary.md"
BACKTEST_RESULTS_FILE = BACKTEST_DIR / "backtest_results.jsonl"
CANDIDATE_DIR = RUNTIME_DIR / "candidate_configs"

MIN_TASK_GAP_SEC = int(os.environ.get("TOP1_WEB_WORKER_MIN_TASK_GAP_SEC", "240"))
POLL_SLEEP_SEC = int(os.environ.get("TOP1_WEB_WORKER_POLL_SLEEP_SEC", "90"))
ARCHIVE_TTL_SEC = int(os.environ.get("TOP1_WEB_WORKER_ARCHIVE_TTL_SEC", "900"))
BACKTEST_TIMEOUT_SEC = int(os.environ.get("TOP1_WEB_WORKER_BACKTEST_TIMEOUT_SEC", "900"))
BACKTESTS_PER_CYCLE = int(os.environ.get("TOP1_WEB_WORKER_BACKTESTS_PER_CYCLE", "3"))
MAX_POLL_ATTEMPTS = int(os.environ.get("TOP1_WEB_WORKER_MAX_POLL_ATTEMPTS", "0"))


BUILTIN_BACKTEST_IDEAS = [
    {
        "id": "fm_h4_baseline_balanced_v3_30d",
        "base_cfg": "obw_platform/configs/h4_freedommoney_hybrid_balanced_v3.yaml",
        "npz": "DB/fast_cache_akela_shortlist_1m_30d.npz",
        "symbol": "FREEDOMMONEY/USDT:USDT",
        "patch": {},
        "argument": "Baseline for the worker H4 lane; needed before judging guessed deltas.",
    },
    {
        "id": "fm_h4_w11c2_A_tail_safe_30d",
        "base_cfg": "obw_platform/configs/h4_freedommoney_hybrid_balanced_v3.yaml",
        "npz": "DB/fast_cache_akela_shortlist_1m_30d.npz",
        "symbol": "FREEDOMMONEY/USDT:USDT",
        "patch": {
            "strategy_params_long.subSellTPPercent": 1.35,
            "strategy_params_short.maxShortInvestPct": 1.05,
        },
        "argument": "Worker consensus first test: reduce long sub-sell target and add only a modest short-side lift without raising long exposure.",
    },
    {
        "id": "fm_h4_w11_range_mid_tail_compression_30d",
        "base_cfg": "obw_platform/configs/h4_freedommoney_hybrid_balanced_v3.yaml",
        "npz": "DB/fast_cache_akela_shortlist_1m_30d.npz",
        "symbol": "FREEDOMMONEY/USDT:USDT",
        "patch": {
            "strategy_params_long.tpPercent": 0.75,
            "strategy_params_long.subSellTPPercent": 1.30,
            "strategy_params_short.maxShortInvestPct": 1.10,
            "strategy_params_short.tpPercent": 0.60,
        },
        "argument": "Midpoint of worker_11's first range, kept as a bolder tail-compression/profit-realization hypothesis for contrast.",
    },
    {
        "id": "fm_v21_det_tail_exposure_compress_30d",
        "base_cfg": "obw_platform/configs/V21_freedommoney_bingx_live_candidate_1m_1y.yaml",
        "npz": "DB/fast_cache_akela_shortlist_1m_30d.npz",
        "symbol": "FREEDOMMONEY/USDT:USDT",
        "patch": {
            "strategy_params_long.maxLongInvestPct": 1.6,
            "strategy_params_long.linearDropPercent": 0.021,
        },
        "argument": "Worker_12 flagged V21 FreedomMoney long maxLongInvestPct=2.4 as hidden-tail risk; compress exposure before profit-chasing.",
    },
    {
        "id": "sup_v21_dd_repair_exposure_spacing_30d",
        "base_cfg": "obw_platform/configs/V21_freedommoney_bingx_live_candidate_1m_1y.yaml",
        "npz": "DB/fast_cache_akela_shortlist_1m_30d.npz",
        "symbol": "SUP/USDT:USDT",
        "patch": {
            "strategy_params_long.maxLongInvestPct": 1.5,
            "strategy_params_long.linearDropPercent": 0.024,
            "strategy_params_short.maxShortInvestPct": 1.0,
        },
        "argument": "SUP was ranked as drawdown-repair, not yield-max; compress exposure and widen spacing.",
    },
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def log(msg: str) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now(timezone.utc).strftime('%H:%M:%SZ')}] {msg}"
    print(line, flush=True)
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def git_text(args: list[str], max_chars: int = 20000) -> str:
    git = r"C:\Users\1\AppData\Local\GitHubDesktop\app-3.5.8\resources\app\git\cmd\git.exe"
    try:
        proc = subprocess.run(
            [git, "-C", str(ROOT), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
        )
        return proc.stdout[-max_chars:]
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def latest_lines(path: Path, limit: int = 120) -> str:
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-limit:])


def dotted_get(data: dict, path: str):
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def dotted_set(data: dict, path: str, value) -> None:
    cur = data
    parts = path.split(".")
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def score_backtest(result: dict) -> float:
    mtm = float(result.get("final_total_pnl", result.get("total_pnl_mtm", 0.0)) or 0.0)
    mdd = abs(float(result.get("mdd_mtm_%", 0.0) or 0.0))
    unreal = abs(float(result.get("final_unrealized_pnl", result.get("unrealized_pnl_total", 0.0)) or 0.0))
    margin = int(result.get("margin_call_events_total", 0) or 0)
    trades = int(result.get("trades_total", 0) or 0)
    trade_penalty = 25.0 if trades < 50 else 0.0
    return (mtm / max(mdd, 1.0)) - (0.01 * unreal) - (100.0 * margin) - trade_penalty


def parse_backtest_json(text: str) -> dict:
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    best = {}
    for idx, char in enumerate(text):
        if char != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            best = obj
    return best


def candidate_path(idea: dict) -> Path:
    return CANDIDATE_DIR / f"{idea['id']}.yaml"


def materialize_candidate_config(idea: dict) -> tuple[Path, dict]:
    import yaml

    base = ROOT / idea["base_cfg"]
    cfg = yaml.safe_load(base.read_text(encoding="utf-8"))
    changes = []
    for dotted, new_value in idea.get("patch", {}).items():
        old_value = dotted_get(cfg, dotted)
        dotted_set(cfg, dotted, new_value)
        changes.append({"path": dotted, "old": old_value, "new": new_value})
    cfg.setdefault("meta", {})
    cfg["meta"]["web_worker_param_idea"] = {
        "id": idea["id"],
        "base_cfg": idea["base_cfg"],
        "argument": idea.get("argument", ""),
        "changes": changes,
        "created_at_utc": utc_now(),
    }
    CANDIDATE_DIR.mkdir(parents=True, exist_ok=True)
    out = candidate_path(idea)
    out.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return out, {"changes": changes}


def load_backtest_results() -> dict[str, dict]:
    results = {}
    if not BACKTEST_RESULTS_FILE.exists():
        return results
    for line in BACKTEST_RESULTS_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        result = row.get("result") or {}
        has_metrics = bool({"total_pnl_mtm", "final_total_pnl"} & set(result.keys()))
        if row.get("status") == "ok" and not has_metrics:
            continue
        if row.get("id"):
            results[row["id"]] = row
    return results


def append_backtest_result(row: dict) -> None:
    BACKTEST_RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with BACKTEST_RESULTS_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def write_backtest_summary() -> None:
    results = sorted(
        load_backtest_results().values(),
        key=lambda row: float(row.get("score", -10**9) or -10**9),
        reverse=True,
    )
    lines = [
        "# Web-worker backtest artifacts",
        "",
        f"updated_at_utc: {utc_now()}",
        "",
        "| rank | id | status | score | mtm | mdd_mtm_% | unrealized | trades | margin_calls | argument |",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for idx, row in enumerate(results, start=1):
        result = row.get("result") or {}
        lines.append(
            "| {rank} | `{id}` | {status} | {score:.4f} | {mtm:.4f} | {mdd:.4f} | {unreal:.4f} | {trades} | {margin} | {argument} |".format(
                rank=idx,
                id=row.get("id", ""),
                status=row.get("status", ""),
                score=float(row.get("score", 0.0) or 0.0),
                mtm=float(result.get("final_total_pnl", result.get("total_pnl_mtm", 0.0)) or 0.0),
                mdd=float(result.get("mdd_mtm_%", 0.0) or 0.0),
                unreal=float(result.get("final_unrealized_pnl", result.get("unrealized_pnl_total", 0.0)) or 0.0),
                trades=int(result.get("trades_total", 0) or 0),
                margin=int(result.get("margin_call_events_total", 0) or 0),
                argument=str(row.get("argument", "")).replace("|", "/")[:240],
            )
        )
    if len(lines) == 6:
        lines.append("|  | no backtests yet |  |  |  |  |  |  |  |  |")
    BACKTEST_SUMMARY_FILE.parent.mkdir(parents=True, exist_ok=True)
    BACKTEST_SUMMARY_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_backtest_idea(idea: dict) -> dict:
    cfg_path, cfg_info = materialize_candidate_config(idea)
    out_dir = BACKTEST_DIR / idea["id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    npz = ROOT / idea["npz"]
    if not npz.exists():
        row = {
            "id": idea["id"],
            "ts": utc_now(),
            "status": "missing_data",
            "argument": idea.get("argument", ""),
            "base_cfg": idea["base_cfg"],
            "candidate_cfg": rel(cfg_path),
            "npz": idea["npz"],
            "symbol": idea.get("symbol", ""),
            "config_changes": cfg_info.get("changes", []),
            "error": f"missing npz: {idea['npz']}",
            "score": -10**9,
            "result": {},
        }
        append_backtest_result(row)
        write_backtest_summary()
        log(f"[backtest] {idea['id']} missing data {idea['npz']}")
        return row

    cmd = [
        sys.executable,
        str(ROOT / "obw_platform" / "backtester_dual_long_short_fast_pack_v2.py"),
        "--cfg", str(cfg_path),
        "--npz", str(npz),
        "--symbol", idea.get("symbol", ""),
        "--export-curves", str(out_dir / "curves.csv"),
    ]
    started = time.time()
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=BACKTEST_TIMEOUT_SEC,
    )
    (out_dir / "backtest.log").write_text(proc.stdout, encoding="utf-8", errors="replace")
    result = parse_backtest_json(proc.stdout)
    status = "ok" if proc.returncode == 0 and result and not result.get("error") else "failed"
    row = {
        "id": idea["id"],
        "ts": utc_now(),
        "status": status,
        "argument": idea.get("argument", ""),
        "base_cfg": idea["base_cfg"],
        "candidate_cfg": rel(cfg_path),
        "npz": idea["npz"],
        "symbol": idea.get("symbol", ""),
        "command": " ".join(cmd),
        "log": rel(out_dir / "backtest.log"),
        "curves": rel(out_dir / "curves.csv"),
        "elapsed_sec": round(time.time() - started, 3),
        "config_changes": cfg_info.get("changes", []),
        "score": score_backtest(result) if status == "ok" else -10**9,
        "result": result,
    }
    append_backtest_result(row)
    write_backtest_summary()
    log(
        "[backtest] {id} {status} score={score:.4f} mtm={mtm} mdd={mdd} unreal={unreal}".format(
            id=idea["id"],
            status=status,
            score=float(row["score"]),
            mtm=result.get("final_total_pnl", result.get("total_pnl_mtm")),
            mdd=result.get("mdd_mtm_%"),
            unreal=result.get("final_unrealized_pnl", result.get("unrealized_pnl_total")),
        )
    )
    return row


def run_due_backtests(state: dict) -> list[dict]:
    done = load_backtest_results()
    pending = [idea for idea in BUILTIN_BACKTEST_IDEAS if idea["id"] not in done]
    if not pending:
        write_backtest_summary()
        return []
    rows = []
    for idea in pending[:max(0, BACKTESTS_PER_CYCLE)]:
        try:
            rows.append(run_backtest_idea(idea))
        except subprocess.TimeoutExpired as exc:
            row = {
                "id": idea["id"],
                "ts": utc_now(),
                "status": "timeout",
                "argument": idea.get("argument", ""),
                "base_cfg": idea["base_cfg"],
                "npz": idea["npz"],
                "symbol": idea.get("symbol", ""),
                "error": f"timeout after {BACKTEST_TIMEOUT_SEC}s",
                "score": -10**9,
                "result": {},
            }
            append_backtest_result(row)
            write_backtest_summary()
            rows.append(row)
            log(f"[backtest] {idea['id']} timeout: {exc}")
        except Exception as exc:
            row = {
                "id": idea["id"],
                "ts": utc_now(),
                "status": "error",
                "argument": idea.get("argument", ""),
                "base_cfg": idea["base_cfg"],
                "npz": idea["npz"],
                "symbol": idea.get("symbol", ""),
                "error": f"{type(exc).__name__}: {exc}",
                "score": -10**9,
                "result": {},
            }
            append_backtest_result(row)
            write_backtest_summary()
            rows.append(row)
            log(f"[backtest] {idea['id']} error: {type(exc).__name__}: {exc}")
    if rows:
        state.setdefault("backtests", [])
        state["backtests"] = (state.get("backtests", []) + [
            {
                "id": row.get("id"),
                "status": row.get("status"),
                "score": row.get("score"),
                "result": row.get("result", {}),
                "argument": row.get("argument", ""),
            }
            for row in rows
        ])[-100:]
    return rows


def write_context_note() -> Path:
    note = RUNTIME_DIR / "TOP1_WEB_WORKER_CONTEXT.md"
    sections = [
        "# top_1 Web-Worker Parameter Loop Context",
        "",
        f"generated_at_utc: {utc_now()}",
        "",
        "## Mission",
        "",
        "Use two web workers to propose and critique better parameter guesses for fresh symbols and current champion configs. The local orchestrator may create runtime-only candidate configs and run bounded backtests. Do not deploy, do not touch live trading, and do not edit production configs directly.",
        "",
        "## Orchestrator Contract",
        "",
        latest_lines(CONTRACT_FILE, 220),
        "",
        "## Local Backtest Artifacts From Worker Ideas",
        "",
        latest_lines(BACKTEST_SUMMARY_FILE, 220),
        "",
        "## Candidate Symbols",
        "",
        "- IDOL/USDT:USDT",
        "- FREEDOMMONEY/USDT:USDT",
        "- MAXXING/USDT:USDT",
        "- SUP/USDT:USDT",
        "- ENA/USDT:USDT as the current V21 live baseline/champion comparison lane",
        "",
        "## Current Config Focus",
        "",
        "- obw_platform/configs/V21_strict_trend_stable_live_static9p38.yaml",
        "- obw_platform/configs/V21_freedommoney_bingx_live_candidate_1m_1y.yaml",
        "- obw_platform/configs/V21_maxxing_bingx_live_candidate_1m_1y.yaml",
        "- obw_platform/configs/V21_static9p38_experimental_long_subtp043_20260511.yaml",
        "- obw_platform/configs/h4_freedommoney_hybrid_balanced_v3.yaml",
        "- obw_platform/configs/h4_freedommoney_ratio_best_v2.yaml",
        "- obw_platform/configs/h4_c002_w9_fee_0002.yaml",
        "",
        "## Optimization Target",
        "",
        "Prefer robust MTM improvement with controlled drawdown, low terminal unrealized exposure, zero or low margin calls, and enough trade count to avoid fragile one-off wins. Treat guesses as hypotheses until local backtests confirm them.",
        "",
        "## Current Git Summary",
        "",
        "```text",
        git_text(["status", "--short"]),
        "```",
        "",
        "## Latest Akela Champion Search",
        "",
        latest_lines(ROOT / "obw_platform" / "meta_strategies" / "akela_meta_short" / "reports" / "latest_champion_search.md", 160),
        "",
        "## Latest Akela Summary",
        "",
        latest_lines(ROOT / "obw_platform" / "meta_strategies" / "akela_meta_short" / "reports" / "latest_summary.md", 160),
        "",
        "## FreedomMoney Handoff",
        "",
        latest_lines(ROOT / "docs" / "freedommoney_handoff" / "AGENT_STATE.md", 180),
        "",
        "## V21 Optimization Report",
        "",
        latest_lines(ROOT / "docs" / "v21_optimization" / "REPORT_2026_05_11.md", 200),
        "",
        "## Personal Run Parameters",
        "",
        latest_lines(ROOT / "run params.txt", 220),
    ]
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("\n".join(sections) + "\n", encoding="utf-8")
    return note


def include_file(path: Path) -> bool:
    if not path.is_file():
        return False
    rel_parts = path.relative_to(ROOT).parts
    excluded = {
        ".git", "node_modules", ".next", "DB", "DEX_REPORTS", "DEX_DATA",
        "__pycache__", "runtime", "tmp_web_worker_bridge_blocked",
        "tmp_web_worker_bridge_dry",
    }
    if any(part in excluded for part in rel_parts):
        return False
    if path.suffix.lower() in {".npz", ".db", ".sqlite", ".zip", ".png", ".jpg", ".log", ".pyc"}:
        return False
    if path.stat().st_size > 180 * 1024:
        return False
    return True


def build_archive() -> Path:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    context_note = write_context_note()
    files: dict[Path, str] = {context_note: "TOP1_WEB_WORKER_CONTEXT.md"}

    roots = [
        ROOT / "continuity",
        ROOT / "docs",
        ROOT / "obw_platform" / "configs",
        ROOT / "obw_platform" / "meta_strategies" / "akela_meta_short",
        ROOT / "obw_platform" / "tuner_plans",
        ROOT / "obw_platform" / "tools",
        ROOT / "scripts",
    ]
    top_files = [
        ROOT / "run params.txt",
        ROOT / "obw_platform" / "auto_tuner_dual_fast_pack.py",
        ROOT / "obw_platform" / "backtester_dual_long_short_fast_pack_v2.py",
        ROOT / "obw_platform" / "backtester_dual_long_short_fast_pack_live_start_slippage_v2.py",
        ROOT / "obw_platform" / "slippage_orderbook_model_v1.py",
    ]
    for path in top_files:
        if path.exists() and include_file(path):
            files[path] = rel(path)
    for root in roots:
        if root.exists():
            for path in sorted(root.rglob("*")):
                if include_file(path):
                    files[path] = rel(path)

    manifest = [
        {"path": arc, "sha256": file_sha256(path), "size": path.stat().st_size}
        for path, arc in sorted(files.items(), key=lambda item: item[1])
    ]
    old = None
    if ARCHIVE_FILE.exists() and ARCHIVE_MANIFEST.exists():
        try:
            old = json.loads(ARCHIVE_MANIFEST.read_text(encoding="utf-8-sig"))
        except Exception:
            old = None
    if old == manifest:
        return ARCHIVE_FILE

    with zipfile.ZipFile(ARCHIVE_FILE, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, arc in sorted(files.items(), key=lambda item: item[1]):
            info = zipfile.ZipInfo(arc)
            info.date_time = (2026, 1, 1, 0, 0, 0)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())
    ARCHIVE_MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"Built context archive: {ARCHIVE_FILE.name} ({ARCHIVE_FILE.stat().st_size // 1024}KB)")
    return ARCHIVE_FILE


def default_state() -> dict:
    return {
        "schema_version": "top1_web_worker_param_loop_v1",
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "cycle": 0,
        "target": "web workers guess stronger parameters for fresh top_1 symbols/configs",
        "workers": {
            worker["id"]: {
                "status": "init",
                "uploaded": False,
                "last_task_sent_at": 0,
                "archive_sent_at": 0,
                "archive_hash": "",
                "archive_name": "",
                "last_response_len": 0,
                "last_task_text": "",
                "sent_tasks": [],
                "responses": [],
                "backoff_until": 0,
            }
            for worker in WORKERS
        },
        "knowledge": [],
        "results": [],
        "backtests": [],
        "poll_attempt": 0,
        "pending_followups": {},
        "ready_for_live": False,
        "next_action": "send_initial_tasks",
    }


def load_state(reset: bool = False) -> dict:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    if reset or not STATE_FILE.exists():
        state = default_state()
        save_state(state)
        return state
    return json.loads(STATE_FILE.read_text(encoding="utf-8-sig"))


def save_state(state: dict) -> None:
    state["updated_at"] = utc_now()
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    export_tree(state)


def export_tree(state: dict) -> None:
    TREE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tree = {
        "schema_version": "top1_web_worker_param_tree_v1",
        "generated_at_utc": utc_now(),
        "summary": {
            "cycle": state.get("cycle", 0),
            "workers_total": len(WORKERS),
            "knowledge_count": len(state.get("knowledge", [])),
            "result_count": len(state.get("results", [])),
            "ready_for_live": False,
            "next_action": state.get("next_action", ""),
        },
        "workers": state.get("workers", {}),
        "recent_knowledge": state.get("knowledge", [])[-20:],
        "recent_results": state.get("results", [])[-20:],
    }
    TREE_FILE.write_text(json.dumps(tree, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def knowledge_from_response(wid: str, response: str) -> list[str]:
    out = []
    for line in response.splitlines():
        text = line.strip()
        if not text:
            continue
        tags = [
            "[PARAM_GUESS]", "[CONFIG_CANDIDATE]", "[HYPOTHESIS]",
            "[TEST_PLAN]", "[SERVER_CHECK]", "[RISK]", "[RANKING]",
            "[RESULT]", "[NEXT_TASK]",
        ]
        if any(tag in text for tag in tags):
            out.append(f"{wid}: {text[:700]}")
    if not out and response.strip():
        out.append(f"{wid}: [SUMMARY] {response.strip()[:700]}")
    return out[:12]


def make_initial_task(worker: dict) -> str:
    role_extra = (
        "You are the hypothesis generator. Produce bold but testable parameter guesses."
        if worker["id"] == "worker_11"
        else "You are the critic. Reject fragile guesses and rank only robust test candidates."
    )
    return f"""TOP_1 WEB-WORKER PARAMETER EXPERIMENT

You are {worker['id']} / {worker['name']}. Parent orchestrator is Marko1. {role_extra}

Use the uploaded context archive only. You cannot run local code. Do not ask for secrets. Do not suggest live trading or deploy.

Goal: infer better parameters for fresh candidate symbols/configs, including current champion/baseline lanes. The local orchestrator will run bounded backtests for defensible ideas and send artifacts back to you.
- IDOL, FREEDOMMONEY, MAXXING, SUP, ENA
- V21 live baseline/static9p38, V21 candidate configs, h4 FreedomMoney configs, and current champion references.

Output format:
[RANKING] top 5 parameter experiment candidates
[PARAM_GUESS] exact config path or config family, parameter names, old value if known, proposed value/range, expected effect
[TEST_PLAN] smallest local backtest/tuner command shape or yearly dataset need
[RISK] overfit/liquidity/tail/unrealized/margin-call risks
[NEXT_TASK] what the other worker or orchestrator should critique next

Optimize for robust MTM improvement, controlled MTM drawdown, low terminal unrealized exposure, zero/low margin calls, and enough trades. Treat guesses as hypotheses, not truth. Every idea must include an argument.
"""


def make_followup_task(worker: dict, state: dict, response_infos: list[dict]) -> str:
    recent = "\n".join(item["text"] for item in state.get("knowledge", [])[-20:])
    backtests = latest_lines(BACKTEST_SUMMARY_FILE, 120)
    if worker["id"] == "worker_11":
        instruction = (
            "Refine parameter guesses after the critic's objections. Produce fewer, sharper candidates with exact ranges."
        )
    else:
        instruction = (
            "Audit the generator's newest guesses. Rank what is worth testing and what should be rejected."
        )
    return f"""TOP_1 PARAMETER LOOP CYCLE {state.get('cycle')}

{instruction}

Recent extracted knowledge:
{recent or '(none yet)'}

Recent local backtest artifacts:
{backtests or '(no local backtests completed yet)'}

Return only useful new information:
[RANKING] ranked candidate experiments
[PARAM_GUESS] exact parameter/value/range
[TEST_PLAN] validation command shape, no execution
[RISK] specific failure mode
[NEXT_TASK] next prompt for the other worker

Do not repeat old generic advice. Do not claim local execution. Do not request live trading.
"""


async def navigate_safe(manager: ChatGPTWorkerManager, worker: dict) -> bool:
    owner = "top1_web_worker_param_loop"
    if not acquire_nav_lock(worker["url"], owner, 60):
        log(f"[{worker['id']}] nav lock denied")
        return False
    try:
        await manager._call_tool("browser_navigate", {"url": worker["url"]})
        await human_sleep(2.0, 0.4, 1.0)
        return True
    finally:
        release_nav_lock(worker["url"], owner)


async def send_task(manager: ChatGPTWorkerManager, worker: dict, state: dict, message: str, force_archive: bool = False) -> bool:
    wid = worker["id"]
    ws = state["workers"][wid]
    if time.time() < float(ws.get("backoff_until", 0) or 0):
        log(f"[{wid}] backoff active")
        return False
    if (time.time() - float(ws.get("last_task_sent_at", 0) or 0)) < MIN_TASK_GAP_SEC and not force_archive:
        log(f"[{wid}] task cooldown")
        return False
    if not await navigate_safe(manager, worker):
        return False
    status = await asyncio.wait_for(manager.get_worker_status(), timeout=20)
    if status != WorkerStatus.IDLE:
        log(f"[{wid}] {status.value} - not sending")
        if status == WorkerStatus.RATE_LIMITED:
            ws["backoff_until"] = time.time() + 2700
        return False

    archive = build_archive()
    archive_hash = file_sha256(archive)
    age = time.time() - float(ws.get("archive_sent_at", 0) or 0)
    need_archive = force_archive or not ws.get("uploaded") or ws.get("archive_hash") != archive_hash or age > ARCHIVE_TTL_SEC
    if need_archive:
        log(f"[{wid}] uploading context archive before task")
        ok = await asyncio.wait_for(manager.upload_file_to_worker(str(archive), message=message), timeout=240)
        if ok:
            ws["uploaded"] = True
            ws["archive_sent_at"] = time.time()
            ws["archive_hash"] = archive_hash
            ws["archive_name"] = archive.name
        else:
            log(f"[{wid}] archive upload failed")
            return False
    else:
        ok = await asyncio.wait_for(manager.send_task_to_worker(message), timeout=60)
    if ok:
        ws["last_task_sent_at"] = time.time()
        ws["last_task_text"] = message[:180]
        ws["sent_tasks"] = (ws.get("sent_tasks", []) + [message[:180]])[-20:]
        ws["status"] = "task_sent"
        log(f"[{wid}] task sent")
    return ok


async def read_worker(manager: ChatGPTWorkerManager, worker: dict, state: dict) -> dict:
    wid = worker["id"]
    info = {"wid": wid, "status": "unknown", "response": "", "new": False}
    if not await navigate_safe(manager, worker):
        return info
    status = await asyncio.wait_for(manager.get_worker_status(), timeout=20)
    info["status"] = status.value
    state["workers"][wid]["worker_status"] = status.value
    if status == WorkerStatus.RATE_LIMITED:
        state["workers"][wid]["backoff_until"] = time.time() + 2700
        return info
    if status != WorkerStatus.IDLE:
        log(f"[{wid}] {status.value}")
        return info
    response = await manager._get_last_response_js()
    info["response"] = response
    new_len = len(response)
    old_len = int(state["workers"][wid].get("last_response_len", 0) or 0)
    has_session_task = bool(state["workers"][wid].get("last_task_sent_at", 0) or 0)
    info["new"] = has_session_task and new_len != old_len and new_len > 0
    state["workers"][wid]["last_response_len"] = new_len
    state["workers"][wid]["status"] = "responded" if new_len else "idle"
    if info["new"]:
        state["workers"][wid]["responses"] = (state["workers"][wid].get("responses", []) + [{
            "ts": utc_now(),
            "len": new_len,
            "excerpt": response[:1200],
        }])[-20:]
        for text in knowledge_from_response(wid, response):
            state["knowledge"].append({"ts": utc_now(), "worker": wid, "text": text})
        state["knowledge"] = state["knowledge"][-200:]
    log(f"[{wid}] idle response_len={new_len} new={info['new']}")
    return info


def local_cycle_result(state: dict, infos: list[dict], backtest_rows: list[dict]) -> None:
    fresh = [i for i in infos if i.get("new")]
    if fresh or backtest_rows:
        state["results"].append({
            "ts": utc_now(),
            "cycle": state.get("cycle", 0),
            "summary": f"new worker outputs from {[i['wid'] for i in fresh]} | backtests={[r.get('id') for r in backtest_rows]}",
            "knowledge_count": len(state.get("knowledge", [])),
        })
    state["results"] = state["results"][-100:]
    state["next_action"] = "send_followups" if fresh or backtest_rows else "wait_for_backoff_or_worker"


def backoff_wait_seconds(state: dict) -> int:
    waits = []
    now = time.time()
    for ws in state.get("workers", {}).values():
        until = float(ws.get("backoff_until", 0) or 0)
        if until > now:
            waits.append(until - now)
    if not waits:
        return POLL_SLEEP_SEC
    return max(5, min(POLL_SLEEP_SEC, int(min(waits))))


async def run_loop(max_cycles: int, reset: bool = False) -> dict:
    state = load_state(reset=reset)
    log(f"top_1 web-worker parameter loop started | max_cycles={max_cycles}")
    async with ChatGPTWorkerManager() as manager:
        while int(state.get("cycle", 0) or 0) < max_cycles:
            state["poll_attempt"] = int(state.get("poll_attempt", 0) or 0) + 1
            log("=" * 58)
            log(f"TOP1 WEB-WORKER PARAM POLL {state['poll_attempt']} | completed_cycles={state.get('cycle', 0)}/{max_cycles}")
            infos = []
            sent_count = 0

            for worker in WORKERS:
                ws = state["workers"][worker["id"]]
                if ws.get("status") == "init":
                    if await send_task(manager, worker, state, make_initial_task(worker), force_archive=True):
                        sent_count += 1
                    await human_sleep(5, 1.0, 2.0)

            pending = dict(state.get("pending_followups", {}) or {})
            for worker in WORKERS:
                wid = worker["id"]
                message = pending.get(wid)
                if not message:
                    continue
                if await send_task(manager, worker, state, message, force_archive=False):
                    sent_count += 1
                    pending.pop(wid, None)
                await human_sleep(5, 1.0, 2.0)
            state["pending_followups"] = pending

            await human_sleep(8, 2.0, 3.0)

            for worker in WORKERS:
                info = await read_worker(manager, worker, state)
                infos.append(info)
                await human_sleep(3.0, 1.0, 1.0)

            backtest_rows = run_due_backtests(state)
            local_cycle_result(state, infos, backtest_rows)

            if any(info.get("new") for info in infos) or backtest_rows:
                pending = dict(state.get("pending_followups", {}) or {})
                for worker in WORKERS:
                    # Send at most one follow-up per completed reasoning cycle and respect cooldown/backoff.
                    message = make_followup_task(worker, state, infos)
                    if await send_task(manager, worker, state, message, force_archive=False):
                        sent_count += 1
                        pending.pop(worker["id"], None)
                    else:
                        pending[worker["id"]] = message
                    await human_sleep(5, 1.0, 2.0)
                state["pending_followups"] = pending

            progressed = bool(sent_count or any(info.get("new") for info in infos) or backtest_rows)
            if progressed:
                state["cycle"] = int(state.get("cycle", 0) or 0) + 1
                log(f"completed reasoning cycle {state['cycle']}/{max_cycles}")
            else:
                wait_s = backoff_wait_seconds(state)
                log(f"no reasoning progress; waiting {wait_s}s without incrementing cycle")

            save_state(state)
            WAKEUP_FLAG.write_text(json.dumps({"ts": utc_now(), "cycle": state["cycle"]}), encoding="utf-8")
            if int(state.get("cycle", 0) or 0) < max_cycles:
                if MAX_POLL_ATTEMPTS and int(state.get("poll_attempt", 0) or 0) >= MAX_POLL_ATTEMPTS:
                    log(f"max poll attempts reached ({MAX_POLL_ATTEMPTS}); stopping early")
                    break
                await asyncio.sleep(backoff_wait_seconds(state) if not progressed else POLL_SLEEP_SEC)
    return state


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-cycles", type=int, default=10)
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()
    state = asyncio.run(run_loop(args.max_cycles, reset=args.reset))
    print(json.dumps({"state": rel(STATE_FILE), "tree": rel(TREE_FILE), "cycle": state.get("cycle")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
