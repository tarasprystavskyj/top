#!/usr/bin/env python3
"""Event watchdog for the top_1 Telegram research loop.

This mirrors the useful part of the DEX event loop on Windows: local jobs and
web-worker artifacts write durable events, and this process wakes the active
Codex coordinator session with a compact resume prompt.

Research-only. It does not import or call live runners, broker clients, order
execution, API keys, or private keys.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SESSION_ID = "019e2cc6-d53b-7331-8aa8-1f4905b31199"
DEFAULT_CONSILIUM_ROOT = (
    ROOT.parent
    / "chrome"
    / "workers_automate"
    / "tmp"
    / "autonomous_consilium_telegram_dca"
)
DEFAULT_COLLECTOR_META = ROOT / "DB" / "telegram_signals_1m_event_windows_720h_bingx.npz.meta.json"
DEFAULT_STATE = ROOT / ".agent" / "control" / "top_event_watchdog_state.json"
DEFAULT_LOG = ROOT / ".agent" / "control" / "top_event_watchdog.log"
DEFAULT_EVENTS = ROOT / ".agent" / "events"
DEFAULT_SINGLE_AGENT_WAKEUP = (
    ROOT / "continuity" / "single_agent_loop_from_dex" / "runtime" / "single_agent_wakeup.flag"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        pass
    return default


def save_json(path: Path, payload: Any) -> None:
    ensure_parent(path)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def log(path: Path, line: str) -> None:
    ensure_parent(path)
    text = f"[{utc_now()}] {line}"
    print(text, flush=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(text + "\n")


def file_key(path: Path) -> str:
    try:
        stat = path.stat()
        return f"{path.resolve()}|{stat.st_mtime_ns}|{stat.st_size}"
    except OSError:
        return str(path.resolve())


def compact_tail(path: Path, max_chars: int = 1800) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"<failed to read {path}: {type(exc).__name__}: {exc}>"
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def discover_agent_events(events_dir: Path, seen: set[str]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if not events_dir.exists():
        return out
    for path in sorted(events_dir.glob("*"), key=lambda p: p.stat().st_mtime if p.exists() else 0):
        if not path.is_file():
            continue
        key = f"agent_event:{file_key(path)}"
        if key in seen:
            continue
        out.append({
            "key": key,
            "type": "agent_event",
            "path": str(path),
            "summary": compact_tail(path, 1600),
        })
    return out


def discover_single_agent_wakeup(path: Path, seen: set[str]) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    key = f"single_agent_wakeup:{file_key(path)}"
    if key in seen:
        return []
    return [{
        "key": key,
        "type": "single_agent_wakeup",
        "path": str(path),
        "summary": compact_tail(path, 1000),
    }]


def discover_collector_status(meta_path: Path, seen: set[str]) -> List[Dict[str, str]]:
    meta = load_json(meta_path, None)
    if not isinstance(meta, dict):
        return []
    status = str(meta.get("status") or "").lower()
    if status not in {"completed", "failed"}:
        return []
    updated = str(meta.get("updated_at") or meta.get("created_at") or "")
    key = f"collector_meta:{meta_path.resolve()}|{status}|{updated}|{meta.get('output_rows')}"
    if key in seen:
        return []
    summary = {
        "status": meta.get("status"),
        "output_npz": meta.get("output_npz"),
        "metadata": str(meta_path),
        "handoff": meta.get("handoff_path"),
        "symbols": meta.get("output_symbols_count"),
        "rows": meta.get("output_rows"),
        "missing_symbols": meta.get("missing_symbols"),
        "failures": meta.get("failures"),
    }
    return [{
        "key": key,
        "type": "collector_status",
        "path": str(meta_path),
        "summary": json.dumps(summary, ensure_ascii=False, indent=2),
    }]


def discover_consilium_responses(root: Path, seen: set[str], stable_sec: float) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if not root.exists():
        return out
    now = time.time()
    for path in sorted(root.glob("*/web_worker_response.md"), key=lambda p: p.stat().st_mtime if p.exists() else 0):
        try:
            stat = path.stat()
        except OSError:
            continue
        if now - stat.st_mtime < stable_sec:
            continue
        key = f"consilium_response:{file_key(path)}"
        if key in seen:
            continue
        out.append({
            "key": key,
            "type": "consilium_web_response",
            "path": str(path),
            "summary": compact_tail(path, 2200),
        })
    return out


def build_resume_message(events: Iterable[Dict[str, str]]) -> str:
    lines = [
        "TOP Telegram watchdog event. Continue the research-only coordination loop.",
        "Do not touch live/daemon/order execution. First inspect the artifacts below, then choose the next paper-only action.",
        "",
    ]
    for idx, event in enumerate(events, start=1):
        lines.extend([
            f"## Event {idx}: {event['type']}",
            f"path: {event['path']}",
            "",
            "```text",
            event.get("summary", "")[:2500],
            "```",
            "",
        ])
    return "\n".join(lines).strip()


def wake_codex(session_id: str, message: str, log_path: Path, dry_run: bool) -> int:
    if dry_run:
        log(log_path, f"dry-run wake session={session_id} chars={len(message)}")
        return 0
    out_path = log_path.with_name("top_event_watchdog_codex_resume.out.log")
    err_path = log_path.with_name("top_event_watchdog_codex_resume.err.log")
    with out_path.open("a", encoding="utf-8") as out, err_path.open("a", encoding="utf-8") as err:
        proc = subprocess.Popen(
            ["codex.cmd", "exec", "resume", session_id, message],
            cwd=str(ROOT),
            text=True,
            stdout=out,
            stderr=err,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    log(
        log_path,
        f"codex resume spawned pid={proc.pid} session={session_id} chars={len(message)} "
        f"stdout={out_path} stderr={err_path}",
    )
    return 0


def check_once(args: argparse.Namespace, state: Dict[str, Any]) -> int:
    seen = set(state.setdefault("seen", []))
    events: List[Dict[str, str]] = []
    events.extend(discover_agent_events(Path(args.events_dir), seen))
    events.extend(discover_single_agent_wakeup(Path(args.single_agent_wakeup), seen))
    events.extend(discover_collector_status(Path(args.collector_meta), seen))
    events.extend(discover_consilium_responses(Path(args.consilium_root), seen, args.consilium_stable_sec))

    if not events:
        state["updated_at"] = utc_now()
        return 0

    # Batch a small number of events so one wake can carry related context.
    batch = events[: args.max_events_per_wake]
    message = build_resume_message(batch)
    rc = wake_codex(args.session_id, message, Path(args.log), args.dry_run)
    if rc == 0:
        for event in batch:
            seen.add(event["key"])
        state["seen"] = sorted(seen)[-1000:]
        state.setdefault("wakeups", []).append({
            "ts": utc_now(),
            "session_id": args.session_id,
            "events": [{"type": e["type"], "path": e["path"]} for e in batch],
            "dry_run": bool(args.dry_run),
        })
        state["wakeups"] = state["wakeups"][-100:]
    else:
        state.setdefault("errors", []).append({"ts": utc_now(), "rc": rc, "events": batch})
        state["errors"] = state["errors"][-50:]
    state["updated_at"] = utc_now()
    return len(batch)


def main() -> int:
    ap = argparse.ArgumentParser(description="top_1 event watchdog")
    ap.add_argument("--session-id", default=DEFAULT_SESSION_ID)
    ap.add_argument("--state", default=str(DEFAULT_STATE))
    ap.add_argument("--log", default=str(DEFAULT_LOG))
    ap.add_argument("--events-dir", default=str(DEFAULT_EVENTS))
    ap.add_argument("--single-agent-wakeup", default=str(DEFAULT_SINGLE_AGENT_WAKEUP))
    ap.add_argument("--collector-meta", default=str(DEFAULT_COLLECTOR_META))
    ap.add_argument("--consilium-root", default=str(DEFAULT_CONSILIUM_ROOT))
    ap.add_argument("--consilium-stable-sec", type=float, default=90.0)
    ap.add_argument("--max-events-per-wake", type=int, default=3)
    ap.add_argument("--sleep-sec", type=float, default=30.0)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    state_path = Path(args.state)
    log_path = Path(args.log)
    state = load_json(state_path, {"schema_version": "top_event_watchdog_v1", "created_at": utc_now(), "seen": []})
    log(log_path, f"watchdog start loop={args.loop} once={args.once} session={args.session_id}")
    while True:
        changed = check_once(args, state)
        save_json(state_path, state)
        if changed:
            log(log_path, f"handled events={changed}")
        if args.once or not args.loop:
            break
        time.sleep(args.sleep_sec)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
