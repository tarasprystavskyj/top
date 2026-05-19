# Telegram DCA Data Unlock Request

Date: 2026-05-17
Mode: paper-only research
Live readiness: false

## Current State

`BLOCKED_BY_DATA`

No Telegram DCA/backtest runs are allowed until this data gate passes.

## Needed To Unlock

Add more historical Telegram signals from the same source/channel as the current 312-row extracted dataset.

Required signal fields:

```text
dt_utc
symbol
side
entry / entry range
stop_loss
take_profit_levels
source_channel
raw_message_id or raw_message_text
```

Required validation artifacts after data is added:

```text
updated_signal_manifest.json
updated_npz_manifest.json
market_coverage_report.csv
signal_csv_sha256
updated_npz_sha256
data_gate_verdict.json
```

Pass conditions:

```text
added_signal_count > 0
market_coverage_rate >= 0.95
schema_compatible == true
no_future_leakage == true
data_gate_status == PASS
```

## After Unlock Only

Allowed first validation runs:

```text
all_49/no-filter control
all-after control
rolling240d_symbol_side_min3_positive
OOS/time-split validation
rolling symbol/state validation
```

Still forbidden:

```text
old Cycle 002 rerun
broad DCA grid
Cycle 003 narrow DCA sweep before validation gate
live execution
paper-live daemon changes
broker/order placement
secrets
deploy state
```
