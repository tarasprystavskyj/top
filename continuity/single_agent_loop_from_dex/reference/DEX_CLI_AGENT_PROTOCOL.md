# CLI Agent Protocol

Updated: 2026-05-11

CLI agents include Codex CLI runs, supervised shell jobs, local Python scripts,
and remote VPS jobs started by the orchestrator. They are the only agents that
may create or modify repository artifacts.

## Responsibilities

CLI agents may:

- inspect repository code, state, reports, and non-secret data artifacts;
- run whitelisted local validation commands;
- run whitelisted data update and paper-live watcher commands;
- create Markdown reports under `DEX_REPORTS/` or `DEX_REPORTS_LOCAL/`;
- update generated state files needed by the autonomous loop;
- write a terminal job result and wake the orchestrator.

CLI agents must not:

- read, print, copy, or modify secrets, private keys, seed phrases, or `.env`;
- perform approvals, swaps, LP mints/burns, or transaction broadcasts;
- promote signed live without explicit human approval;
- treat browser worker text as proof of local execution.

## Job Manifest

Every long-running or remote job should have a small manifest before it starts:

```json
{
  "job_id": "bio_usdc_s2_freshness_YYYYMMDDTHHMMSSZ",
  "kind": "data_update|paper_live_watch|comparison|report",
  "target": "BIO/USDC router V6 S2_PAPER_LIVE_PROOF",
  "command": "sanitized command without secrets",
  "cwd": "c:/python_scripts/dex or /var/www/vps2.happyuser.info/dex",
  "started_at": "UTC ISO timestamp",
  "expected_outputs": [
    "path/to/report.md",
    "path/to/updated_artifact"
  ],
  "wake_on_exit": true,
  "paper_only": true,
  "signed_live_allowed": false
}
```

The terminal result should state:

- exit status;
- start and end timestamps;
- output paths;
- source data timestamps and hashes when relevant;
- whether it woke the orchestrator;
- whether the S2 gate item is satisfied, missing, invalid, or failed.

## Wakeup Contract

When a CLI job finishes, it wakes the orchestrator by writing `wakeup.flag` with
a JSON reason:

```json
{
  "reason": "job_finished: bio_usdc_s2_freshness_YYYYMMDDTHHMMSSZ",
  "ts": "UTC ISO timestamp",
  "artifact": "DEX_REPORTS/live_readiness/example.md"
}
```

The watchdog may also wake the orchestrator when a browser worker finishes.
Both wakeup sources are valid, but the orchestrator decides the next action.

## S2 BIO/USDC Allowed Work

For the current gate, useful CLI work is limited to:

- corrected/fresh BIO/USDC NPZ update;
- paper-live watcher freshness and virtual LP state verification;
- same-window paper-live versus backtest comparison;
- all-idle reason aggregation;
- config/hash identity report;
- paper-only and no-signer/no-broadcast proof;
- concise gate report that maps evidence to pass/fail/continue.

Broad pool discovery is paused unless the current candidate fails or a human
changes the target.

