#!/usr/bin/env python3
"""Minimal top_1 single-agent loop runtime.

This is a safe MVP adapted from the DEX loop contract. It runs only fixed,
read-only whitelisted jobs and writes state/result/tree JSON artifacts.
"""

import argparse
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME = ROOT / "continuity" / "single_agent_loop_from_dex" / "runtime"
SCHEMA_STATE = "single_agent_loop_v1"
SCHEMA_RESULT = "single_agent_job_result_v1"
SCHEMA_TREE = "single_agent_tree_v1"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def runtime_dir() -> Path:
    return Path(os.environ.get("SINGLE_AGENT_RUNTIME_DIR", str(DEFAULT_RUNTIME))).resolve()


def state_path() -> Path:
    return runtime_dir() / "single_agent_state.json"


def wakeup_path() -> Path:
    return runtime_dir() / "single_agent_wakeup.flag"


def tree_path() -> Path:
    return runtime_dir() / "ui_data" / "single_agent_tree.json"


def jobs_dir() -> Path:
    return runtime_dir() / "jobs"


def ensure_dirs() -> None:
    runtime_dir().mkdir(parents=True, exist_ok=True)
    jobs_dir().mkdir(parents=True, exist_ok=True)
    tree_path().parent.mkdir(parents=True, exist_ok=True)


def default_state(target="top_1 local single-agent loop MVP"):
    return {
        "schema_version": SCHEMA_STATE,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "cycle": 0,
        "phase": "read_state",
        "target": target,
        "ready_for_live": False,
        "jobs": [],
        "knowledge": [],
        "decisions": [],
        "safety_stops": [],
        "next_action": "enqueue_or_wait",
    }


def load_state():
    ensure_dirs()
    path = state_path()
    if not path.exists():
        state = default_state()
        save_state(state)
        return state
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_state(state):
    ensure_dirs()
    state["updated_at"] = utc_now()
    state_path().write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    export_tree(state)


def write_wakeup(reason: str, artifact: str = "") -> None:
    payload = {"reason": reason, "ts": utc_now(), "artifact": artifact}
    wakeup_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_knowledge(state, text, artifact=""):
    state.setdefault("knowledge", []).append({"ts": utc_now(), "text": text, "artifact": artifact})
    state["knowledge"] = state["knowledge"][-50:]


def export_tree(state):
    jobs = state.get("jobs", [])
    knowledge = state.get("knowledge", [])
    tree = {
        "schema_version": SCHEMA_TREE,
        "generated_at": utc_now(),
        "summary": {
            "cycle": state.get("cycle", 0),
            "phase": state.get("phase", ""),
            "target": state.get("target", ""),
            "next_action": state.get("next_action", ""),
            "job_count": len(jobs),
            "running_jobs": len([j for j in jobs if j.get("status") == "running"]),
            "queued_jobs": len([j for j in jobs if j.get("status") == "queued"]),
            "ready_for_live": False,
        },
        "tree": {
            "id": "single_agent:root",
            "type": "single_agent_loop",
            "label": "top_1 single-agent loop",
            "children": [
                {
                    "id": "single_agent:jobs",
                    "type": "jobs",
                    "label": f"Jobs ({len(jobs)})",
                    "children": [
                        {
                            "id": f"job:{job.get('job_id')}",
                            "type": "job",
                            "label": f"{job.get('kind')} [{job.get('status')}]",
                            "data": job,
                        }
                        for job in jobs[-25:]
                    ],
                },
                {
                    "id": "single_agent:knowledge",
                    "type": "knowledge",
                    "label": f"Knowledge ({len(knowledge)})",
                    "children": [
                        {
                            "id": f"knowledge:{idx}",
                            "type": "knowledge_item",
                            "label": item.get("text", "")[:120],
                            "data": item,
                        }
                        for idx, item in enumerate(knowledge[-25:])
                    ],
                },
            ],
        },
        "state": state,
    }
    tree_path().write_text(json.dumps(tree, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def safe_git_status():
    proc = subprocess.run(
        ["git", "status", "--short"],
        cwd=str(ROOT),
        universal_newlines=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return proc.returncode, proc.stdout


def safe_continuity_scan():
    paths = [
        ROOT / "continuity" / "AGENTS.md",
        ROOT / "continuity" / "MAP.md",
        ROOT / "continuity" / "lines" / "codex-workflow.md",
        ROOT / "continuity" / "lines" / "strategy-research.md",
    ]
    chunks = []
    for path in paths:
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="replace")
            lines = [line for line in text.splitlines() if "Next Useful Move" in line or "Drift Risks" in line or line.startswith("# ")]
            chunks.append(f"## {rel(path)}\n" + "\n".join(lines[:40]))
    return 0, "\n\n".join(chunks)


def safe_akela_meta_summary():
    path = ROOT / "obw_platform" / "meta_strategies" / "akela_meta_short" / "reports" / "latest_summary.md"
    if not path.exists():
        return 1, f"missing: {rel(path)}"
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return 0, "\n".join(lines[:160]) + "\n"


def env_truthy(name):
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def safe_ai_single_turn_bridge():
    prompt_path = jobs_dir() / f"ai_bridge_prompt_{stamp()}.md"
    manifest_path = jobs_dir() / f"ai_bridge_manifest_{stamp()}.json"
    allow = env_truthy("SINGLE_AGENT_ALLOW_AI_TURN")
    dry_run = env_truthy("SINGLE_AGENT_AI_DRY_RUN")
    prompt = [
        "# top_1 Single-Agent AI Bridge Prompt",
        "",
        "This prompt is generated by the safe single-agent loop bridge.",
        "Default mode is read/report only. Do not touch live/deploy/trading/YAML/DB/NPZ.",
        "Do not read `.env` or secrets.",
        "",
        "Reference command, not executed by default:",
        "",
        "```bash",
        "scripts/freedom_claude_loop_single_agent_v3.sh",
        "```",
        "",
        "Required result format:",
        "",
        "```text",
        "ACTIONS_EXECUTED: <number>",
        "FILES_CHANGED: <list>",
        "VALIDATION: <command and result>",
        "NEXT_ACTION: <one concrete action>",
        "```",
    ]
    prompt_path.write_text("\n".join(prompt) + "\n", encoding="utf-8")
    manifest = {
        "schema_version": "single_agent_ai_bridge_manifest_v1",
        "created_at": utc_now(),
        "allow_ai_turn": allow,
        "dry_run": dry_run,
        "reference_command": "scripts/freedom_claude_loop_single_agent_v3.sh",
        "prompt_path": rel(prompt_path),
        "safety": {
            "reads_env": False,
            "invokes_ai": False,
            "touches_live_deploy_trading": False,
            "auto_rotation": False,
        },
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if not allow:
        return 2, json.dumps(
            {
                "status": "blocked",
                "reason": "AI bridge disabled; set SINGLE_AGENT_ALLOW_AI_TURN=1 to permit dry-run bridge preparation.",
                "manifest": rel(manifest_path),
                "prompt": rel(prompt_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    if dry_run:
        return 0, json.dumps(
            {
                "status": "dry_run",
                "reason": "AI bridge manifest and prompt generated; no Claude/Codex process invoked.",
                "manifest": rel(manifest_path),
                "prompt": rel(prompt_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    return 3, json.dumps(
        {
            "status": "blocked",
            "reason": "Real AI invocation is intentionally disabled in this MVP; use SINGLE_AGENT_AI_DRY_RUN=1 for validation.",
            "manifest": rel(manifest_path),
            "prompt": rel(prompt_path),
        },
        ensure_ascii=False,
        indent=2,
    )


def safe_web_worker_loop_bridge():
    prompt_path = jobs_dir() / f"web_worker_bridge_prompt_{stamp()}.md"
    manifest_path = jobs_dir() / f"web_worker_bridge_manifest_{stamp()}.json"
    allow = env_truthy("SINGLE_AGENT_ALLOW_WEB_WORKERS")
    dry_run = env_truthy("SINGLE_AGENT_WEB_WORKER_DRY_RUN")
    prompt = [
        "# top_1 Optional Web-Worker Loop Bridge",
        "",
        "This bridge preserves the DEX browser-worker orchestration option on top of",
        "the safer default single-agent loop. It is intentionally not part of",
        "auto-rotation.",
        "",
        "Use this mode only on a local workstation with Chrome remote debugging,",
        "ChatGPT worker conversations, and the `chrome/workers_automate` tooling",
        "available. Do not run it on the production UI host unless explicitly",
        "approved.",
        "",
        "Reference contracts:",
        "",
        "- `continuity/single_agent_loop_from_dex/reference/DEX_AUTONOMOUS_LOOP_PROTOCOL.md`",
        "- `continuity/single_agent_loop_from_dex/reference/DEX_CLI_AGENT_PROTOCOL.md`",
        "- `continuity/single_agent_loop_from_dex/reference/dex_orchestrator_reference.py`",
        "- `continuity/single_agent_loop_from_dex/reference/dex_worker_watchdog_reference.py`",
        "",
        "Required safety shape:",
        "",
        "- keep the current read/report loop as default;",
        "- require explicit human approval before using browser workers;",
        "- use separate runtime state/tree/log files for web-worker mode;",
        "- upload context before worker tasks;",
        "- never send live trading, deploy, `.env`, secret, or exchange actions to workers;",
        "- treat worker outputs as advisory evidence, not authority;",
        "- convert repeated worker requests into whitelisted local jobs.",
        "",
        "Suggested local-only command pattern:",
        "",
        "```powershell",
        "$env:SINGLE_AGENT_ALLOW_WEB_WORKERS='1'",
        "$env:SINGLE_AGENT_WEB_WORKER_DRY_RUN='1'",
        "python scripts/single_agent_loop.py --enqueue web_worker_loop_bridge --once",
        "```",
    ]
    prompt_path.write_text("\n".join(prompt) + "\n", encoding="utf-8")
    manifest = {
        "schema_version": "single_agent_web_worker_bridge_manifest_v1",
        "created_at": utc_now(),
        "allow_web_workers": allow,
        "dry_run": dry_run,
        "prompt_path": rel(prompt_path),
        "reference_files": [
            "continuity/single_agent_loop_from_dex/reference/DEX_AUTONOMOUS_LOOP_PROTOCOL.md",
            "continuity/single_agent_loop_from_dex/reference/DEX_CLI_AGENT_PROTOCOL.md",
            "continuity/single_agent_loop_from_dex/reference/dex_orchestrator_reference.py",
            "continuity/single_agent_loop_from_dex/reference/dex_worker_watchdog_reference.py",
            "continuity/single_agent_loop_from_dex/reference/dex_orchestrator_tree_server_reference.py",
        ],
        "safety": {
            "auto_rotation": False,
            "invokes_browser_workers": False,
            "requires_local_chrome_debug_port": True,
            "touches_live_deploy_trading": False,
            "reads_env": False,
        },
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if not allow:
        return 2, json.dumps(
            {
                "status": "blocked",
                "reason": "Web-worker bridge disabled; set SINGLE_AGENT_ALLOW_WEB_WORKERS=1 and SINGLE_AGENT_WEB_WORKER_DRY_RUN=1 for local dry-run preparation.",
                "manifest": rel(manifest_path),
                "prompt": rel(prompt_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    if dry_run:
        return 0, json.dumps(
            {
                "status": "dry_run",
                "reason": "Web-worker bridge manifest and prompt generated; no browser worker or Chrome action invoked.",
                "manifest": rel(manifest_path),
                "prompt": rel(prompt_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    return 3, json.dumps(
        {
            "status": "blocked",
            "reason": "Real browser-worker invocation is intentionally not implemented in the safe MVP; use the generated manifest and reference files for explicit local orchestration.",
            "manifest": rel(manifest_path),
            "prompt": rel(prompt_path),
        },
        ensure_ascii=False,
        indent=2,
    )


WHITELIST = {
    "git_status_summary": {
        "runner": safe_git_status,
        "command": "git status --short",
        "summary": "Read-only git worktree summary.",
    },
    "continuity_scan": {
        "runner": safe_continuity_scan,
        "command": "read selected continuity docs",
        "summary": "Read-only continuity drift scan.",
    },
    "akela_meta_summary": {
        "runner": safe_akela_meta_summary,
        "command": "read Akela meta latest_summary.md",
        "summary": "Read-only Akela meta report summary.",
    },
    "ai_single_turn_bridge": {
        "runner": safe_ai_single_turn_bridge,
        "command": "prepare guarded AI single-turn bridge manifest",
        "summary": "Optional AI-action bridge, disabled by default and excluded from auto-rotation.",
    },
    "web_worker_loop_bridge": {
        "runner": safe_web_worker_loop_bridge,
        "command": "prepare guarded browser-worker loop bridge manifest",
        "summary": "Optional DEX-style browser-worker bridge, disabled by default and excluded from auto-rotation.",
    },
}

ROTATION = ["continuity_scan", "akela_meta_summary", "git_status_summary"]


def make_job(kind, target):
    if kind not in WHITELIST:
        raise SystemExit(f"job kind is not whitelisted: {kind}")
    return {
        "job_id": f"job_{stamp()}_{kind}",
        "kind": kind,
        "target": target,
        "status": "queued",
        "command": WHITELIST[kind]["command"],
        "manifest": None,
        "pid": None,
        "started_at": None,
        "ended_at": None,
        "result": None,
        "error": None,
        "trigger_count": 0,
        "wake_on_exit": True,
    }


def enqueue(state, kind, target):
    state.setdefault("jobs", []).append(make_job(kind, target))
    state["next_action"] = "run_queued_job"
    state.setdefault("decisions", []).append({"ts": utc_now(), "action": "enqueue", "kind": kind, "target": target})


def terminal_jobs(state):
    return [j for j in state.get("jobs", []) if j.get("status") in {"succeeded", "failed", "failed_or_no_result"}]


def pending_jobs(state):
    return [j for j in state.get("jobs", []) if j.get("status") in {"queued", "running"}]


def rotate_kind(state):
    counts = {}
    for job in state.get("jobs", []):
        counts[job.get("kind")] = counts.get(job.get("kind"), 0) + 1
    min_count = min([counts.get(kind, 0) for kind in ROTATION])
    for offset in range(len(ROTATION)):
        idx = (int(state.get("cycle") or 0) + offset) % len(ROTATION)
        kind = ROTATION[idx]
        if counts.get(kind, 0) == min_count:
            return kind
    return ROTATION[0]


def auto_enqueue_if_idle(state):
    if pending_jobs(state):
        return False
    kind = rotate_kind(state)
    enqueue(state, kind, "auto-rotation read/report job")
    state.setdefault("decisions", []).append({"ts": utc_now(), "action": "auto_rotate", "kind": kind})
    return True


def run_one_job(state):
    running = [j for j in state.get("jobs", []) if j.get("status") == "running"]
    if running:
        state["phase"] = "wait_running_job"
        state["next_action"] = "wait"
        return False
    queued = [j for j in state.get("jobs", []) if j.get("status") == "queued"]
    if not queued:
        state["phase"] = "idle"
        state["next_action"] = "enqueue_or_wait"
        return False

    job = queued[0]
    kind = job["kind"]
    spec = WHITELIST.get(kind)
    if spec is None:
        job["status"] = "failed"
        job["error"] = f"not whitelisted: {kind}"
        append_knowledge(state, f"Blocked non-whitelisted job {kind}")
        return True

    job["status"] = "running"
    job["started_at"] = utc_now()
    job["trigger_count"] = int(job.get("trigger_count") or 0) + 1
    save_state(state)

    started = time.time()
    manifest_path = jobs_dir() / f"{job['job_id']}.manifest.json"
    out_path = jobs_dir() / f"{job['job_id']}.out.txt"
    result_path = jobs_dir() / f"{job['job_id']}.result.json"
    manifest = {
        "schema_version": "single_agent_job_manifest_v1",
        "job_id": job["job_id"],
        "kind": kind,
        "target": job.get("target", ""),
        "command": job.get("command", ""),
        "cwd": str(ROOT),
        "started_at": job["started_at"],
        "expected_outputs": [rel(out_path), rel(result_path)],
        "wake_on_exit": bool(job.get("wake_on_exit", True)),
        "signed_live_allowed": False,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    job["manifest"] = rel(manifest_path)
    try:
        exit_code, output = spec["runner"]()
        status = "succeeded" if exit_code == 0 else "failed"
    except Exception as exc:
        exit_code = 1
        output = f"{type(exc).__name__}: {exc}"
        status = "failed"

    out_path.write_text(output, encoding="utf-8", errors="replace")
    ended = time.time()
    result = {
        "schema_version": SCHEMA_RESULT,
        "job_id": job["job_id"],
        "kind": kind,
        "target": job.get("target", ""),
        "status": status,
        "started_at": job["started_at"],
        "ended_at": utc_now(),
        "duration_sec": round(ended - started, 3),
        "exit_code": exit_code,
        "manifest_path": rel(manifest_path),
        "output_path": rel(out_path),
        "summary": (output.strip().splitlines() or [spec["summary"]])[0][:240],
        "wakeup_written": bool(job.get("wake_on_exit", True)),
    }
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    job["status"] = status
    job["ended_at"] = result["ended_at"]
    job["result"] = rel(result_path)
    job["error"] = None if status == "succeeded" else result["summary"]
    append_knowledge(state, f"{kind} {status}: {result['summary']}", rel(result_path))
    if job.get("wake_on_exit", True):
        write_wakeup(f"job_finished:{job['job_id']}", rel(result_path))
    state["phase"] = "job_terminal"
    state["next_action"] = "collect_or_enqueue"
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="top_1 single-agent loop MVP")
    ap.add_argument("--init", action="store_true", help="initialize state and tree")
    ap.add_argument("--enqueue", choices=sorted(WHITELIST), help="enqueue a whitelisted job")
    ap.add_argument("--target", default="manual run", help="job target text")
    ap.add_argument("--once", action="store_true", help="run one loop cycle")
    ap.add_argument("--loop", action="store_true", help="run repeated cycles")
    ap.add_argument("--sleep", type=float, default=30.0, help="loop sleep seconds")
    ap.add_argument("--max-cycles", type=int, default=0, help="optional loop cap")
    ap.add_argument("--auto-rotate", action="store_true", help="enqueue safe read/report jobs when idle")
    ap.add_argument("--allow-ai-turn", action="store_true", help="allow the optional AI bridge job to prepare dry-run artifacts")
    ap.add_argument("--ai-dry-run", action="store_true", help="make the optional AI bridge generate manifest/prompt without invoking AI")
    ap.add_argument("--allow-web-workers", action="store_true", help="allow the optional web-worker bridge job to prepare dry-run artifacts")
    ap.add_argument("--web-worker-dry-run", action="store_true", help="make the optional web-worker bridge generate manifest/prompt without invoking browser workers")
    args = ap.parse_args()

    if args.allow_ai_turn:
        os.environ["SINGLE_AGENT_ALLOW_AI_TURN"] = "1"
    if args.ai_dry_run:
        os.environ["SINGLE_AGENT_AI_DRY_RUN"] = "1"
    if args.allow_web_workers:
        os.environ["SINGLE_AGENT_ALLOW_WEB_WORKERS"] = "1"
    if args.web_worker_dry_run:
        os.environ["SINGLE_AGENT_WEB_WORKER_DRY_RUN"] = "1"

    env_max_cycles = int(os.environ.get("SINGLE_AGENT_MAX_CYCLES", "0") or "0")
    if env_max_cycles and not args.max_cycles:
        args.max_cycles = env_max_cycles

    state = load_state()
    if args.init:
        state = default_state()
        append_knowledge(state, "initialized top_1 single-agent loop state")
        save_state(state)

    if args.enqueue:
        enqueue(state, args.enqueue, args.target)
        save_state(state)

    cycles = 0
    while args.once or args.loop:
        state = load_state()
        state["cycle"] = int(state.get("cycle") or 0) + 1
        if args.auto_rotate:
            auto_enqueue_if_idle(state)
        ran = run_one_job(state)
        save_state(state)
        cycles += 1
        if args.once:
            break
        if args.max_cycles and cycles >= args.max_cycles:
            break
        if not ran:
            time.sleep(args.sleep)

    print(json.dumps({"state": rel(state_path()), "tree": rel(tree_path()), "terminal_jobs": len(terminal_jobs(state))}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
