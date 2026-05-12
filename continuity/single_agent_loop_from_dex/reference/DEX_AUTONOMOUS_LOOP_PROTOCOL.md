# Autonomous Loop Protocol

Updated: 2026-05-11

The DEX agent system should be a closed, isolated loop:

```text
orchestrator
  -> browser workers for analysis
  -> CLI/server jobs for evidence
  -> watchdog/wakeup on completion
  -> orchestrator decision
```

The loop is not closed if the orchestrator only keeps asking browser workers to
repeat missing evidence. Missing evidence must become a server job or a wait on
an already-running job.

Browser-worker context is part of loop correctness. A worker task sent without a
fresh archive is low-value unless it is only asking about prior chat text. The
orchestrator therefore refreshes `dex_project_for_workers.zip` before task
dispatch when the archive hash changed, no verified upload exists, or the
archive is older than its TTL.

## State Machine

### 1. Read state

The orchestrator reads:

- `dex_orchestrator_state.json`;
- latest worker responses;
- latest job manifests and terminal reports;
- current S2 reports under `DEX_REPORTS/live_readiness/`;
- current paper-live artifacts under `DEX_REPORTS/paper_live_bio_macro_router_v1/`.

### 2. Classify gaps

Worker text and reports are mapped into gate items:

- source identity and freshness;
- corrected BIO/USDC metadata;
- watcher advancement;
- decision log completeness;
- virtual LP state persistence;
- fee/inventory/total PnL split;
- same-window paper-vs-backtest comparison;
- strict_pass failed-check breakdown;
- V6 config/hash identity;
- paper-only/no-signer/no-broadcast proof.

### 3. Decide action

The orchestrator chooses exactly one primary action per cycle:

- send distinct analysis tasks to idle workers;
- start a whitelisted CLI/server job;
- wait for a running job or generating worker;
- write a concise gate report;
- stop if safety is violated or the target is invalid.

### 4. Wakeup

The watchdog writes `wakeup.flag` when browser workers finish or all workers are
idle long enough. CLI/server jobs also write `wakeup.flag` when they reach a
terminal condition.

### 5. Commit/report

Useful terminal artifacts are committed or at least listed in the final cycle
report. Runtime noise and browser snapshots are not decision artifacts.

## Current Closure Jobs

The orchestrator maps repeated worker gaps into whitelisted jobs. Browser
workers can request evidence, but only the local/Codex side may execute jobs.

Current whitelisted job kinds:

- `bio_usdc_s2_evidence_bundle`: triggered by `[DATA_REQUEST]`,
  `[WORKER_NEEDS]`, or `[ORCHESTRATOR_ACTION]`; builds the hash-bound S2
  evidence bundle and wakes the orchestrator on completion.
- `bio_usdc_s2_extract_checks`: triggered by `[SERVER_CHECK]` or
  `[S2_BUNDLE_INTEGRITY_VALIDATION_PLAN]` after an evidence bundle exists;
  extracts worker-requested identity, freshness, artifact, router, and risk
  fields into `extracted_checks.json` and a short report.

If any job is `queued` or `running`, the orchestrator should wait instead of
sending another sterile worker prompt. Once the job result is collected, the
next worker task must include the refreshed archive.

## Empathy / Traction Metrics

For clean orchestrator experiments, use a separate instance:

```powershell
$env:DEX_ORCH_INSTANCE="empathy"
$env:DEX_WORKER_SET="clean45"
python dex_worker_watchdog.py
python dex_orchestrator.py
```

This writes separate state/log/fifo/wakeup files and uses clean browser workers
4 and 5. The experiment should be judged on two metrics:

- Worker-to-orchestrator traction: workers should explicitly state
  `[WORKER_NEEDS]`, `[DATA_REQUEST]`, or `[INTERNET_DATA_REQUEST]`; the
  orchestrator should convert that into a local/server/internet data action or
  record why it cannot.
- Branch exhaustion awareness: if the current BIO/USDC branch repeats blockers
  without new evidence, the orchestrator should classify it as `ACTIVE`,
  `BLOCKED_BY_DATA`, or `EXHAUSTED_FOR_NOW` and request ranked next-candidate
  data when appropriate.

Sterile worker answers are treated as a protocol failure when they do not tell
the orchestrator what data would let the worker do more useful analysis.

Minimum job queue fields:

```json
{
  "job_id": "string",
  "kind": "string",
  "target": "BIO/USDC router V6 S2_PAPER_LIVE_PROOF",
  "status": "queued|running|succeeded|failed|cancelled",
  "command": "sanitized command",
  "started_at": null,
  "ended_at": null,
  "output_report": "path/to/report.md",
  "wake_on_exit": true
}
```

## Visual State Export

Every `save_state` writes `ui_data/orchestrator_tree_<instance>.json` using
schema `orchestrator_tree_v1`. It is intentionally a JSON tree first and a UI
second, so a future 3D tree renderer can reuse the same data.

Local MVP UI:

```powershell
python ui/orchestrator_tree_server.py
```

Default URL: `http://127.0.0.1:8765/?instance=empathy`

The UI shows:

- top-level summary: instance, cycle, phase, worker count, job count, server
  check count, next action;
- tree branches for workers, whitelisted jobs, and recent knowledge;
- selected-node raw JSON for precise inspection.

## Safety Stops

The orchestrator must stop, not retry, if any artifact shows:

- signed transaction path enabled;
- private key or signer loaded;
- approval/swap/LP mint/burn broadcast path reachable;
- source data identity invalid after correction;
- metadata decimals/token order invalid;
- unbounded retry loop on the same failed job.
