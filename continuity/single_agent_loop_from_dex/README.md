# Single-Agent Loop Handoff From DEX

Updated: 2026-05-12

This packet is a reference handoff for adapting the DEX autonomous loop pattern
to `top_1` without browser/web workers.

## What To Build

Create a closed one-agent local loop:

```text
single_loop_orchestrator
  -> reads repository state, continuity notes, reports, and job manifests
  -> asks one Codex/CLI decision step what to do next
  -> starts exactly one whitelisted local job when evidence is missing
  -> waits while jobs run
  -> collects terminal job results
  -> writes JSON tree state for UI/API inspection
  -> commits concise state/report artifacts when useful
  -> sleeps, or wakes early when watchdog sees a terminal condition
```

The important transfer from DEX is not the browser worker layer. The important
transfer is the contract:

- persistent JSON state;
- whitelisted jobs only;
- terminal job result files;
- `wakeup.flag` for early continuation;
- JSON tree export as the canonical UI/API data model;
- explicit safety stops and no silent retries around the same failed job.

## Reference Files

- `reference/DEX_AUTONOMOUS_LOOP_PROTOCOL.md` - original loop contract.
- `reference/DEX_CLI_AGENT_PROTOCOL.md` - job/result/wakeup contract.
- `reference/dex_orchestrator_reference.py` - reference state machine; includes
  worker code that should be removed for `top_1`.
- `reference/dex_worker_watchdog_reference.py` - wakeup/watchdog precedent.
- `reference/dex_orchestrator_tree_server_reference.py` - local JSON tree UI/API
  precedent.

## Adaptation Rules For `top_1`

Do not copy the DEX orchestrator verbatim. Use it as a pattern.

Recommended local file names:

- `single_agent_loop.py`
- `single_agent_watchdog.py`
- `ui/single_agent_tree_server.py` or reuse the existing `UI/` backend if that
  is clearly cheaper.
- `ui_data/single_agent_tree.json`
- `single_agent_state.json`
- `single_agent_loop.out.txt`
- `single_agent_wakeup.flag`
- `orchestrator_job_result_*.json`

The first usable MVP should not tune strategies directly inside the
orchestrator. Repeated actions should become whitelisted jobs. The orchestrator
should only choose, start, wait, collect, summarize, and stop.

## top_1 MVP Implementation

The local MVP files are:

- `scripts/single_agent_loop.py`
- `scripts/single_agent_watchdog.py`
- `scripts/single_agent_control_room.sh`
- `continuity/single_agent_loop_from_dex/TOP1_SINGLE_AGENT_LOOP_CONTRACT.md`

The control room runs a productive read/report rotation when idle:

```bash
scripts/single_agent_control_room.sh
```

tmux management wrapper:

```bash
scripts/single_agent_tmux.sh start
scripts/single_agent_tmux.sh status
scripts/single_agent_tmux.sh tail
scripts/single_agent_tmux.sh stop
```

For bounded smoke validation:

```bash
SINGLE_AGENT_RUNTIME_DIR=/tmp/top1_single_agent_loop_smoke \
SINGLE_AGENT_MAX_CYCLES=2 \
SINGLE_AGENT_SLEEP_SECONDS=0 \
scripts/single_agent_control_room.sh
```

The resulting API-ready tree is:

```text
$SINGLE_AGENT_RUNTIME_DIR/ui_data/single_agent_tree.json
```

Default runtime tree:

```text
continuity/single_agent_loop_from_dex/runtime/ui_data/single_agent_tree.json
```

Default log:

```text
continuity/single_agent_loop_from_dex/runtime/logs/control_room.log
```

## Optional AI Bridge

The loop is read/report only unless an AI bridge job is manually queued. The
bridge is not part of auto-rotation.

Blocked-by-default check:

```bash
SINGLE_AGENT_RUNTIME_DIR=/tmp/top1_ai_bridge_blocked \
python3 scripts/single_agent_loop.py --init --enqueue ai_single_turn_bridge --once
```

Dry-run only, without invoking Claude/Codex:

```bash
SINGLE_AGENT_RUNTIME_DIR=/tmp/top1_ai_bridge_dry \
SINGLE_AGENT_ALLOW_AI_TURN=1 \
SINGLE_AGENT_AI_DRY_RUN=1 \
python3 scripts/single_agent_loop.py --init --enqueue ai_single_turn_bridge --once
```

The future reference command is
`scripts/freedom_claude_loop_single_agent_v3.sh`, but this MVP does not execute
it.

## Optional Web-Worker Bridge

The DEX browser-worker orchestration pattern is preserved as a local-only
option, but it is not part of the default `top_1` loop. The production-safe
loop remains single-agent read/report rotation.

Blocked-by-default check:

```bash
SINGLE_AGENT_RUNTIME_DIR=/tmp/top1_web_worker_bridge_blocked \
python3 scripts/single_agent_loop.py --init --enqueue web_worker_loop_bridge --once
```

Dry-run only, without navigating Chrome or sending worker tasks:

```bash
SINGLE_AGENT_RUNTIME_DIR=/tmp/top1_web_worker_bridge_dry \
python3 scripts/single_agent_loop.py \
  --init \
  --allow-web-workers \
  --web-worker-dry-run \
  --enqueue web_worker_loop_bridge \
  --once
```

This writes a manifest and prompt that point at the DEX reference contracts and
worker/watchdog code. Real browser-worker execution must stay explicit and
local: it requires Chrome remote debugging, `chrome/workers_automate`, known
worker conversations, and human approval. Do not run browser-worker mode on the
production UI host by accident.

## Initial Whitelisted Job Candidates

For `top_1`, start with conservative read/report jobs:

- inspect current git status and summarize uncommitted user/runtime changes;
- scan `continuity/` and update the relevant line file only;
- generate or refresh a promotion table for strategy research;
- run a smoke backtest command already accepted by the repo;
- generate a backtest integrity report for the current candidate;
- refresh UI JSON data from existing reports.

Do not let the loop touch live trading, deploy, `.env`, secrets, exchange
credentials, or production config promotion without explicit human approval.

## State Schema Minimum

```json
{
  "schema_version": "single_agent_loop_v1",
  "cycle": 0,
  "phase": 1,
  "target": "short current objective",
  "ready_for_live": false,
  "jobs": [],
  "knowledge": [],
  "decisions": [],
  "next_action": "read_state"
}
```

Minimum job fields:

```json
{
  "job_id": "string",
  "kind": "string",
  "target": "string",
  "status": "queued|running|succeeded|failed|failed_or_no_result",
  "command": "sanitized command",
  "pid": null,
  "started_at": null,
  "ended_at": null,
  "result": null,
  "error": null,
  "trigger_count": 0,
  "wake_on_exit": true
}
```

## Expected Output From The `top_1` Agent

The agent should first report what it already changed in `top_1`, then either:

1. implement the MVP loop in a narrow patch, or
2. explain the blocker and write the smallest useful plan under
   `continuity/lines/codex-workflow.md` or a new dedicated line file.

Required report-back:

- files changed;
- files intentionally not touched;
- validation command run;
- result;
- remaining risk;
- next smallest useful move.
