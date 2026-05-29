# Rolling Neighbor Variants Results

Date: 2026-05-16
Mode: paper-only research
Live readiness: false

## Objective

Test bounded neighboring variants around the strongest causal-looking selector:

`rolling180d_symbol_side_min3_positive`

No broad DCA grid was run. Each variant used base no-DCA TP1 execution only.

## Pre-Run Audit

Before this run, filter row counts and DCA add usage were audited:

- `cycle_002_audit/filter_csv_row_count_audit.csv`
- `cycle_002_audit/dca_add_usage_by_symbol_side.csv`
- `cycle_002_audit/cycle_002_pre_run_audit.md`

Only these filters were flagged as all_49-equivalent:

```text
all_49.csv: 312 rows
zone_pct_le_8.csv: 312 rows
zone_pct_le_10.csv: 312 rows
```

## Variants Tested

```text
rolling120d_symbol_side_min3_positive
rolling120d_symbol_side_min4_positive
rolling180d_symbol_side_min3_positive
rolling180d_symbol_side_min4_positive
rolling240d_symbol_side_min3_positive
rolling240d_symbol_side_min4_positive
```

Each was tested on:

```text
split60_after
split70_after
```

## Results

Best variant:

```text
rolling240d_symbol_side_min3_positive

split60:
  opened_signals: 30
  mtm_pnl_pct: +0.6898%
  mtm_mdd_pct: -0.6077%

split70:
  opened_signals: 23
  mtm_pnl_pct: +0.4985%
  mtm_mdd_pct: -0.6089%
```

Original center variant:

```text
rolling180d_symbol_side_min3_positive

split60:
  opened_signals: 25
  mtm_pnl_pct: +0.5522%
  mtm_mdd_pct: -0.6083%

split70:
  opened_signals: 20
  mtm_pnl_pct: +0.4344%
  mtm_mdd_pct: -0.6090%
```

All tested rolling symbol-side variants were positive on both splits, but all remained below the 50-opened-trade gate.

## Interpretation

- The rolling symbol-side prior state is the most stable edge candidate found in this lane.
- Extending the window from 180d to 240d improved sample size and PnL without breaking split70.
- Minimum 4 prior trades reduces sample too much.
- DCA is not the source of edge; no-DCA TP1 remains the cleanest execution for this filtered cohort.

## Decision

Promotion remains closed.

Next research should focus on sample size:

1. Add more historical static Telegram signals if available.
2. Re-run `rolling240d_symbol_side_min3_positive` as the primary candidate.
3. Keep all-after controls and all_49 controls mandatory.
4. Do not tune live, do not touch paper-live, and do not expand full-universe DCA grids.
