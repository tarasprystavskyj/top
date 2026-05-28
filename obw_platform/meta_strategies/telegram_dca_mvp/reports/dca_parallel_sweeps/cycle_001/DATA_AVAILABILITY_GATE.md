# Data Availability Gate

Date: 2026-05-16
Mode: paper-only research
Live readiness: false

## Objective

Check whether local workspace contains more static Telegram signal history that can increase the sample size for the rolling symbol-side prior filter.

## Result

No local signal CSV with more than 312 rows was found.

Largest signal files:

```text
312 rows: telegram_standard_bt_bundle/telegram_signal_standard_bt/telegram_signals_extracted.csv
312 rows: DB/telegram_signal_standard_bt/telegram_signals_extracted.csv
312 rows: worker_bundle / local_test_bundle copies
312 rows: external_handoffs copies
```

Other Telegram CSVs are filtered/report derivatives:

```text
283 rows: telegram_signals_extracted_no_RENDER.csv
221 rows: telegram_signals_bar_touches_zone_full49.csv
166 rows: telegram_signals_close_in_zone_full49.csv
83 rows: telegram_signals_raw_edge_min5.csv
65 rows: telegram_signals_raw_and_dca_edge_min5.csv
```

## Interpretation

The current local static signal universe remains capped at 312 extracted signals and 256 opened trades in the best all_49 DCA run.

The strongest causal-looking filter found so far:

```text
rolling240d_symbol_side_min3_positive
split60 opened: 30
split70 opened: 23
```

This is below the 50-opened-trade promotion gate. Without more historical static Telegram signals, the lane is sample-limited.

## Decision

Promotion remains closed.

Next useful action requires one of:

1. More historical Telegram signals from the same source/channel.
2. A broader static signal archive from equivalent channels, clearly separated by source.
3. A formal decision to keep this as a research-only micro-edge candidate with no promotion.

Do not continue broad DCA tuning on the same 312 signals. It has already failed as the source of edge.
