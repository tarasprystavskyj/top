# Prior-Only Filter Sweep Results

Date: 2026-05-16
Mode: paper-only research
Live readiness: false

## What Ran

Input filters were generated from the best full-universe DCA trade stream:

`all_49__adds1__cap2x__tp1__wedge/telegram_dca_trades.csv`

Filter CSVs:

- `prior_symbol_min5_positive.csv`: 51 signals
- `prior_symbol_side_min5_positive_diagnostic.csv`: 24 signals
- `prior_symbol_min5_positive_excluding_worst_symbols.csv`: 49 signals

Then the same representative DCA configs from the diagnostic filter batch were run:

- base no-DCA TP1 edge weights
- 1 add / 1.5x / TP1 edge weights
- 1 add / 1.5x / TP3 half weights
- 2 adds / 2x / TP2 thirds

Output:

- `prior_filter_runs/prior_filter_sweep_summary.csv`
- `prior_filter_runs/prior_filter_sweep_summary.json`

## Results

Best run overall:

```text
run_id: prior_symbol_min5_positive_excluding_worst_symbols__base_adds0_cap1_tp1_edge
signals_total: 49
opened_signals: 49
mtm_pnl_pct: +0.8728%
mtm_mdd_pct: -0.7909%
mtm_to_mdd: 1.1035
dca_fill_count: 0
```

This is diagnostic only because it excludes a worst-symbol list derived from full-sample contribution. That exclusion is not a deployable causal rule yet.

Best causal prior-only run with at least 50 opened trades:

```text
run_id: prior_symbol_min5_positive__base_adds0_cap1_tp1_edge
signals_total: 51
opened_signals: 51
mtm_pnl_pct: +0.1554%
mtm_mdd_pct: -0.7909%
mtm_to_mdd: 0.1965
dca_fill_count: 0
```

Best symbol-side diagnostic:

```text
run_id: prior_symbol_side_min5_positive_diagnostic__base_adds0_cap1_tp1_edge
signals_total: 24
opened_signals: 24
mtm_pnl_pct: +0.3384%
mtm_mdd_pct: -0.7817%
mtm_to_mdd: 0.4329
dca_fill_count: 0
```

This remains below the 50-opened-trade gate.

## Interpretation

- The first genuinely causal candidate is `prior_symbol_min5_positive` with 51 opened trades and small positive PnL.
- DCA additions degraded these prior-filtered subsets. The best runs used base no-DCA TP1 exits.
- The strongest-looking result, `prior_symbol_min5_positive_excluding_worst_symbols`, is not deployable as-is because the worst-symbol list was derived after seeing the full run.
- `prior_symbol_side_min5_positive_diagnostic` is promising but too small.

## Next Gate

Do not promote yet.

Next paper-only step:

1. Convert `prior_symbol_min5_positive` into a first-class causal signal filter that can be reproduced directly from prior trades.
2. Split by time:
   - early segment to learn/activate prior symbol state
   - later segment to evaluate
3. Keep all_49 and no-filter control visible.
4. Re-run only base no-DCA TP1 and one conservative DCA variant; broad DCA grids are not justified.
