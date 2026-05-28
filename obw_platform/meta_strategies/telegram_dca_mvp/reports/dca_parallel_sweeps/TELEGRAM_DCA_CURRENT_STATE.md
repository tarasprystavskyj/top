# Telegram DCA Current State

Date: 2026-05-17
Mode: paper-only research
Live readiness: false

Current branch state: `BLOCKED_BY_DATA`

Current data gate: `FAIL`

Current max parallelism: `0`

Do not run additional DCA/backtest sweeps on the current 312 extracted Telegram signals.

Primary candidate:

```text
rolling240d_symbol_side_min3_positive
execution: base no-DCA TP1
split60 opened: 30
split70 opened: 23
required opened per validation split: >=50
```

Blocking reason:

```text
No local Telegram signal history larger than 312 extracted rows was found.
The best candidate is positive but sample-limited.
```

Already-created gate artifacts:

```text
CYCLE_003_EVIDENCE_GATE_REQUEST.json
latest_metrics_extract.csv
DATA_UNLOCK_CRITERIA.json
DATA_UNLOCK_REQUEST.md
DATA_UNLOCK_REQUEST.json
cycle_001/latest_artifact_manifest.json
cycle_001/latest_evidence_digest.md
cycle_001/data_gate_verdict.json
cycle_001/branch_verdict.json
cycle_001/exact_missing_data_fields.json
cycle_001/required_data_window.md
cycle_001/blocked_backtests_until_data_gate_passes.txt
```

Allowed next action:

```text
Add more historical Telegram signals from the same source/channel, then revalidate the primary rolling candidate with all-after and all_49/no-filter controls.
```

Blocked:

```text
broad DCA grids
old Cycle 002 reruns
live execution
paper-live daemon changes
secrets
deploy state
broker/order placement
```
