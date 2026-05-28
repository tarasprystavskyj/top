# Rolling240d Symbol-Side Min3 Candidate Spec

Date: 2026-05-16
Mode: paper-only research
Live readiness: false

## Candidate

`rolling240d_symbol_side_min3_positive`

This is the strongest causal-looking static Telegram signal selector found in the current local dataset.

## Rule

For each candidate Telegram signal, evaluate only trades strictly before that signal timestamp.

Select the signal only when all conditions are true:

1. Same `symbol`.
2. Same `side`.
3. Lookback window: previous 240 days.
4. Prior opened-trade count in that symbol-side bucket is at least 3.
5. Prior realized/MTM contribution for that bucket is positive.

Execution for validation is base no-DCA TP1. DCA additions are not part of this candidate.

## Current Evidence

Source universe:

```text
telegram_standard_bt_bundle/telegram_signal_standard_bt/telegram_signals_extracted.csv
```

Current local universe size:

```text
312 extracted signals
256 opened trades in best all_49 DCA run
```

Result files:

```text
obw_platform/meta_strategies/telegram_dca_mvp/reports/dca_parallel_sweeps/cycle_001/rolling_neighbor_variants/rolling_neighbor_sweep_summary.csv
obw_platform/meta_strategies/telegram_dca_mvp/reports/dca_parallel_sweeps/cycle_001/ROLLING_NEIGHBOR_VARIANTS_RESULTS.md
```

Observed split results:

```text
split60_after:
  opened_signals: 30
  mtm_pnl_pct: +0.6898%
  mtm_mdd_pct: -0.6077%
  mtm_to_mdd: 1.1350

split70_after:
  opened_signals: 23
  mtm_pnl_pct: +0.4985%
  mtm_mdd_pct: -0.6089%
  mtm_to_mdd: 0.8186
```

Controls from prior time-split work were negative after both splits:

```text
split60 all-after: 102 opened, mtm_pnl_pct: -1.1727%, mtm_mdd_pct: -1.9544%
split70 all-after: 76 opened, mtm_pnl_pct: -1.8585%, mtm_mdd_pct: -1.9678%
```

## Why This Candidate Survived

- Positive on both split60 and split70.
- Better sample size than the 120d and 180d neighboring variants.
- Min3 preserves more trades than min4 without breaking split70.
- DCA sweeps did not create edge; no-DCA TP1 is cleaner for this selected cohort.
- Full-universe and broad filter controls stayed negative.

## Blockers

Promotion remains closed.

The candidate has too few opened trades:

```text
split60 opened: 30
split70 opened: 23
promotion gate: >=50 opened trades on validation split
```

The local workspace does not currently contain a larger raw Telegram signal archive:

```text
obw_platform/meta_strategies/telegram_dca_mvp/reports/dca_parallel_sweeps/cycle_001/DATA_AVAILABILITY_GATE.md
```

## Next Loop Direction

Do:

1. Keep `rolling240d_symbol_side_min3_positive` as the primary candidate.
2. Revalidate it only when more historical Telegram signals are added.
3. Keep mandatory controls: all-after, all_49, and no-filter static replay.
4. Track opened count, PnL, MDD, and PnL/MDD for each split.

Do not:

1. Promote to live or paper-live.
2. Run broad full-universe DCA grids on the same 312 signals.
3. Treat full-sample worst-symbol exclusions as causal.
4. Merge this into live execution or daemon code.
