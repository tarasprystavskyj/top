#!/usr/bin/env python3
"""Watchdog for scripts/single_agent_loop.py.

The watchdog is intentionally small: it marks stale running jobs as terminal and
writes a wakeup flag. It does not start trading, tuning, fetching, or deploy.
"""

import argparse
import json
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME = ROOT / "continuity" / "single_agent_loop_from_dex" / "runtime"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def runtime_dir() -> Path:
    return Path(os.environ.get("SINGLE_AGENT_RUNTIME_DIR", str(DEFAULT_RUNTIME))).resolve()


def state_path() -> Path:
    return runtime_dir() / "single_agent_state.json"


def wakeup_path() -> Path:
    return runtime_dir() / "single_agent_wakeup.flag"


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def is_pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def parse_ts(value):
    if not value:
        return None
    raw = value.replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
    return None


def save_state(state):
    state["updated_at"] = utc_now()
    state_path().write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_wakeup(reason: str, artifact: str = "") -> None:
    runtime_dir().mkdir(parents=True, exist_ok=True)
    payload = {"reason": reason, "ts": utc_now(), "artifact": artifact}
    wakeup_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def check_once(max_running_sec: float) -> int:
    if not state_path().exists():
        return 0
    state = json.loads(state_path().read_text(encoding="utf-8-sig"))
    changed = 0
    now = time.time()
    for job in state.get("jobs", []):
        if job.get("status") != "running":
            continue
        started = parse_ts(job.get("started_at"))
        stale = started is not None and (now - started) > max_running_sec
        pid_dead = job.get("pid") is not None and not is_pid_alive(job.get("pid"))
        no_pid = job.get("pid") is None
        if stale or pid_dead or no_pid:
            job["status"] = "failed_or_no_result"
            job["ended_at"] = utc_now()
            job["error"] = "watchdog marked running job terminal"
            state.setdefault("knowledge", []).append(
                {
                    "ts": utc_now(),
                    "text": f"watchdog marked {job.get('job_id')} failed_or_no_result",
                    "artifact": "",
                }
            )
            changed += 1
            write_wakeup(f"watchdog_terminal:{job.get('job_id')}", rel(state_path()))
    if changed:
        save_state(state)
    return changed


def main() -> int:
    ap = argparse.ArgumentParser(description="top_1 single-agent watchdog MVP")
    ap.add_argument("--once", action="store_true", help="run one watchdog check")
    ap.add_argument("--loop", action="store_true", help="run continuously")
    ap.add_argument("--sleep", type=float, default=30.0)
    ap.add_argument("--max-running-sec", type=float, default=1800.0)
    args = ap.parse_args()

    if not args.once and not args.loop:
        args.once = True

    total = 0
    while True:
        total += check_once(args.max_running_sec)
        if args.once:
            break
        time.sleep(args.sleep)
    print(json.dumps({"changed": total, "state": rel(state_path()), "wakeup": rel(wakeup_path())}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
