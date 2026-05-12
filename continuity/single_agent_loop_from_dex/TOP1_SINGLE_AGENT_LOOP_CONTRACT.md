# top_1 Single-Agent Loop Contract

Updated: 2026-05-11 UTC

This is the `top_1` adaptation of the DEX loop contract. It intentionally
removes browser workers. The first implementation is a local Codex/CLI loop
that can run whitelisted read/report jobs, persist state, emit terminal job
results, write a wakeup flag, and export an API-ready JSON tree.

## Scope

Allowed for the MVP:

- inspect git status;
- inspect continuity files;
- inspect already-generated Akela meta summaries;
- write loop state under `continuity/single_agent_loop_from_dex/runtime/`;
- write API JSON under `continuity/single_agent_loop_from_dex/runtime/ui_data/`.

Forbidden for the MVP:

- reading `.env` or secrets;
- touching live trading, deploy, runner, production YAML, DB, NPZ, or logs;
- starting tuners, fetchers, or backtests;
- changing backtest, slippage, fee, exchange, liquidation, or strategy math.

## Runtime Files

Default runtime directory:

```text
continuity/single_agent_loop_from_dex/runtime/
```

Files:

- `single_agent_state.json`
- `single_agent_wakeup.flag`
- `ui_data/single_agent_tree.json`
- `jobs/<job_id>.manifest.json`
- `jobs/<job_id>.result.json`
- `jobs/<job_id>.out.txt`

## State Machine

1. Load or initialize state.
2. Collect terminal result files.
3. If a job is running, wait.
4. If a job is queued, run exactly one whitelisted job.
5. Export JSON tree.
6. Write wakeup flag on terminal job result.
7. Stop if a job repeatedly fails or if a non-whitelisted action is requested.

## Whitelisted MVP Jobs

- `git_status_summary`: read-only `git status --short`.
- `continuity_scan`: read-only scan of continuity docs.
- `akela_meta_summary`: read-only summary of the current Akela meta report.
- `ai_single_turn_bridge`: optional guarded AI-action bridge. It is disabled by
  default and excluded from auto-rotation.

The whitelist is intentionally small. Future jobs can be added only as explicit
entries with a fixed command/function and forbidden path checks.

## Productive Rotation

The control-room wrapper runs:

```bash
scripts/single_agent_control_room.sh
```

It calls `scripts/single_agent_loop.py --loop --auto-rotate`. When the state has
no queued or running job, the loop enqueues one safe read/report job from this
rotation:

1. `continuity_scan`
2. `akela_meta_summary`
3. `git_status_summary`

Bounded smoke runs are supported without touching repo runtime:

```bash
SINGLE_AGENT_RUNTIME_DIR=/tmp/top1_single_agent_loop_smoke \
SINGLE_AGENT_MAX_CYCLES=2 \
SINGLE_AGENT_SLEEP_SECONDS=0 \
scripts/single_agent_control_room.sh
```

The wrapper does not source `.env` files and does not start trading, deployment,
fetching, tuning, backtesting, or live runners.

## Optional AI-Action Bridge

The safe loop is read/report only by default. The optional bridge exists only so
the state machine can represent a future single-agent AI turn as a whitelisted
job with manifest/result files.

Manual blocked check:

```bash
SINGLE_AGENT_RUNTIME_DIR=/tmp/top1_ai_bridge_blocked \
python3 scripts/single_agent_loop.py --init --enqueue ai_single_turn_bridge --once
```

Expected result: terminal job with `status=failed` and a safety message saying
the AI bridge is disabled.

Dry-run check:

```bash
SINGLE_AGENT_RUNTIME_DIR=/tmp/top1_ai_bridge_dry \
SINGLE_AGENT_ALLOW_AI_TURN=1 \
SINGLE_AGENT_AI_DRY_RUN=1 \
python3 scripts/single_agent_loop.py --init --enqueue ai_single_turn_bridge --once
```

Expected result: terminal job with `status=succeeded`, plus a generated bridge
manifest and prompt under `$SINGLE_AGENT_RUNTIME_DIR/jobs/`. No Claude/Codex
process is invoked.

Reference command for a future real bridge:

```bash
scripts/freedom_claude_loop_single_agent_v3.sh
```

The MVP does not execute that command. Real AI invocation remains intentionally
blocked even when `SINGLE_AGENT_ALLOW_AI_TURN=1` unless dry-run is enabled.

## Optional Web-Worker Bridge

The DEX browser-worker layer remains available as a deliberately separate
local-only option. It is useful when a workstation can run Chrome remote
debugging and known worker conversations, but it must not replace the default
safe single-agent rotation.

The whitelisted job is:

- `web_worker_loop_bridge`: writes a manifest and prompt for a DEX-style
  browser-worker loop. It does not navigate Chrome or send worker tasks.

Blocked-by-default check:

```bash
SINGLE_AGENT_RUNTIME_DIR=/tmp/top1_web_worker_bridge_blocked \
python3 scripts/single_agent_loop.py --init --enqueue web_worker_loop_bridge --once
```

Dry-run preparation:

```bash
SINGLE_AGENT_RUNTIME_DIR=/tmp/top1_web_worker_bridge_dry \
python3 scripts/single_agent_loop.py \
  --init \
  --allow-web-workers \
  --web-worker-dry-run \
  --enqueue web_worker_loop_bridge \
  --once
```

Required boundaries:

- default `scripts/single_agent_control_room.sh` never auto-enqueues this job;
- real browser-worker execution requires explicit human approval;
- use separate runtime/log/tree files for web-worker mode;
- do not expose `.env`, secrets, live trading, deploy actions, production YAML
  promotion, DB mutation, or exchange credentials to workers;
- worker outputs are advisory evidence, not authority.

## tmux Wrapper

Use `scripts/single_agent_tmux.sh` to manage the read/report loop:

```bash
scripts/single_agent_tmux.sh start
scripts/single_agent_tmux.sh status
scripts/single_agent_tmux.sh tail
scripts/single_agent_tmux.sh stop
```

Defaults:

- tmux session: `top1_single_agent`
- runtime: `continuity/single_agent_loop_from_dex/runtime/`
- log: `continuity/single_agent_loop_from_dex/runtime/logs/control_room.log`
- JSON tree: `continuity/single_agent_loop_from_dex/runtime/ui_data/single_agent_tree.json`

Bounded tmux smoke:

```bash
SINGLE_AGENT_TMUX_SESSION=top1_single_agent_smoke \
SINGLE_AGENT_RUNTIME_DIR=/tmp/top1_single_agent_tmux_smoke \
SINGLE_AGENT_MAX_CYCLES=2 \
SINGLE_AGENT_SLEEP_SECONDS=0 \
SINGLE_AGENT_INIT_ON_START=1 \
scripts/single_agent_tmux.sh start

scripts/single_agent_tmux.sh status
```

The tmux wrapper only starts `scripts/single_agent_control_room.sh`; it does not
source `.env`, start live jobs, or touch production strategy artifacts.

## Job Result Contract

```json
{
  "schema_version": "single_agent_job_result_v1",
  "job_id": "job_YYYYMMDDTHHMMSSZ_kind",
  "kind": "git_status_summary",
  "target": "short human-readable target",
  "status": "succeeded|failed|failed_or_no_result",
  "started_at": "UTC ISO timestamp",
  "ended_at": "UTC ISO timestamp",
  "duration_sec": 0.0,
  "exit_code": 0,
  "manifest_path": "continuity/single_agent_loop_from_dex/runtime/jobs/job.manifest.json",
  "output_path": "continuity/single_agent_loop_from_dex/runtime/jobs/job.out.txt",
  "summary": "short terminal result",
  "wakeup_written": true
}
```

## JSON Tree Contract

`ui_data/single_agent_tree.json` uses schema `single_agent_tree_v1` and is the
canonical UI/API surface. A UI should read this file instead of scraping logs.
