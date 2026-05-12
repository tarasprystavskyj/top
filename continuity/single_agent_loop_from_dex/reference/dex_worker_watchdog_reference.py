#!/usr/bin/env python3
"""
DEX worker watchdog.

Polls the three ChatGPT DEX worker conversations and wakes dex_orchestrator.py
early by writing dex/wakeup.flag.  If a worker transitions from generating to
idle, its id is appended to dex/worker_fifo.json so the orchestrator reads that
conversation first.
"""

import asyncio
import json
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_DIR = Path(__file__).parent
sys.path.insert(0, str(REPO_DIR.parent / "chrome" / "workers_automate"))

from chatgpt_worker_manager import (  # noqa: E402
    ChatGPTWorkerManager,
    WorkerStatus,
    acquire_nav_lock,
    human_sleep,
    release_nav_lock,
)
from dex_orchestrator import FIFO_FILE, LOG_FILE as ORCH_LOG_FILE, ORCH_INSTANCE, WAKEUP_FLAG, WORKER_SET, WORKERS  # noqa: E402


WATCHDOG_SUFFIX = "" if ORCH_INSTANCE == "main" else f"_{ORCH_INSTANCE}"
LOG_FILE = REPO_DIR / f"dex_worker_watchdog{WATCHDOG_SUFFIX}.log"

POLL_SEC_BASE = 90
MIN_NAV_GAP_SEC = 120
IDLE_WAKEUP_SEC = 180
IDLE_SKIP_THRESHOLD = 3


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%SZ")
    line = f"[DEX-WD {ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def jitter_sleep_sec() -> float:
    return POLL_SEC_BASE * random.uniform(0.7, 1.3)


async def check_worker_status(manager: ChatGPTWorkerManager) -> WorkerStatus:
    try:
        return await asyncio.wait_for(manager.get_worker_status(), timeout=12)
    except asyncio.TimeoutError:
        log("  check_status timeout -> UNKNOWN")
        return WorkerStatus.UNKNOWN
    except Exception as e:
        log(f"  check_status error -> UNKNOWN: {e}")
        return WorkerStatus.UNKNOWN


def append_fifo(worker_ids: list[str]) -> None:
    try:
        fifo = json.loads(FIFO_FILE.read_text(encoding="utf-8-sig")) if FIFO_FILE.exists() else []
    except Exception:
        fifo = []
    for wid in worker_ids:
        if wid not in fifo:
            fifo.append(wid)
    FIFO_FILE.write_text(json.dumps(fifo), encoding="utf-8")


def wake(reason: str) -> None:
    if WAKEUP_FLAG.exists():
        return
    WAKEUP_FLAG.write_text(
        json.dumps({"reason": reason, "ts": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )
    log(f"wakeup.flag written: {reason}")


async def main() -> None:
    enabled = [w for w in WORKERS if w.get("enabled")]
    log(f"DEX watchdog started | instance={ORCH_INSTANCE} worker_set={WORKER_SET} workers={[w['id'] for w in enabled]}")
    was_generating = {w["id"]: None for w in enabled}
    idle_since = {w["id"]: None for w in enabled}
    idle_skip_cnt = {w["id"]: 0 for w in enabled}
    last_nav_time = {w["id"]: 0.0 for w in enabled}

    while True:
        try:
            just_finished: list[str] = []
            any_generating = False
            async with ChatGPTWorkerManager() as manager:
                shuffled = list(enabled)
                random.shuffle(shuffled)

                for worker in shuffled:
                    wid = worker["id"]
                    now = time.time()

                    if now - last_nav_time[wid] < MIN_NAV_GAP_SEC:
                        log(f"[{wid}] nav cooldown")
                        continue

                    if (
                        was_generating[wid] is False
                        and idle_skip_cnt[wid] > 0
                        and idle_skip_cnt[wid] % IDLE_SKIP_THRESHOLD != 0
                    ):
                        idle_skip_cnt[wid] += 1
                        log(f"[{wid}] idle skip #{idle_skip_cnt[wid]}")
                        continue

                    if not acquire_nav_lock(worker["url"], "dex_watchdog", MIN_NAV_GAP_SEC):
                        log(f"[{wid}] nav lock denied")
                        continue

                    try:
                        await manager._call_tool("browser_navigate", {"url": worker["url"]})
                        last_nav_time[wid] = time.time()
                        await human_sleep(2.0, 0.5, 1.2)
                        status = await check_worker_status(manager)
                    finally:
                        release_nav_lock(worker["url"], "dex_watchdog")

                    generating = status == WorkerStatus.GENERATING
                    prev = was_generating[wid]

                    if status == WorkerStatus.UNKNOWN:
                        log(f"[{wid}] UNKNOWN")
                        continue
                    if status == WorkerStatus.RATE_LIMITED:
                        log(f"[{wid}] RATE_LIMITED")
                        any_generating = True
                        continue
                    if status == WorkerStatus.LOGIN_REQUIRED:
                        log(f"[{wid}] LOGIN_REQUIRED")
                        continue

                    if prev is True and not generating:
                        log(f"[{wid}] FINISHED")
                        just_finished.append(wid)
                        idle_skip_cnt[wid] = 0
                    elif generating:
                        log(f"[{wid}] generating")
                        any_generating = True
                        idle_skip_cnt[wid] = 0
                        idle_since[wid] = None
                    else:
                        idle_skip_cnt[wid] += 1
                        log(f"[{wid}] idle")

                    idle_since[wid] = None if generating else (idle_since[wid] or time.time())
                    was_generating[wid] = generating
                    await human_sleep(3.0, 1.0, 1.5)

            if just_finished:
                append_fifo(sorted(just_finished))
                wake(f"fifo: {sorted(just_finished)}")
            elif not any_generating:
                idle_secs = [
                    time.time() - idle_since[w["id"]]
                    for w in enabled
                    if idle_since.get(w["id"]) is not None
                ]
                if len(idle_secs) == len(enabled) and min(idle_secs) >= IDLE_WAKEUP_SEC:
                    wake("all_idle_3min")
                    idle_since = {w["id"]: None for w in enabled}

        except Exception as e:
            log(f"watchdog cycle error: {type(e).__name__}: {e}")

        sleep_sec = jitter_sleep_sec()
        log(f"sleeping {sleep_sec:.0f}s")
        await asyncio.sleep(sleep_sec)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
