# Time-Split Prior Filter Results

Date: 2026-05-16
Mode: paper-only research
Live readiness: false

## Objective

Validate whether the first causal-looking filter, `prior_symbol_min5_positive`, survives a simple time split.

The selector state is built chronologically from prior DCA trades only. Evaluation filters include only signals after the split date.

## Splits

```text
split60: 2025-10-03T11:12:00+00:00
split70: 2025-11-21T11:54:00+00:00
```

Generated filters:

```text
split60_all_after_control: 123 signals
split60_prior_symbol_min5_positive_oos: 38 signals
split60_prior_symbol_side_min5_positive_oos_diagnostic: 15 signals

split70_all_after_control: 95 signals
split70_prior_symbol_min5_positive_oos: 29 signals
split70_prior_symbol_side_min5_positive_oos_diagnostic: 11 signals
```

Configs tested:

```text
base_adds0_cap1_tp1_edge
dca1_cap1p5_tp1_edge
```

## Results

Best OOS prior-symbol filter:

```text
split60_prior_symbol_min5_positive_oos__base_adds0_cap1_tp1_edge
signals_total: 38
opened_signals: 38
mtm_pnl_pct: +0.0227%
mtm_mdd_pct: -0.7169%
mtm_to_mdd: 0.0316
```

Same filter on later split:

```text
split70_prior_symbol_min5_positive_oos__base_adds0_cap1_tp1_edge
signals_total: 29
opened_signals: 29
mtm_pnl_pct: -0.1571%
mtm_mdd_pct: -0.7182%
mtm_to_mdd: -0.2188
```

Controls:

```text
split60_all_after_control__base_adds0_cap1_tp1_edge
signals_total: 123
opened_signals: 102
mtm_pnl_pct: -1.1727%
mtm_mdd_pct: -1.9544%

split70_all_after_control__base_adds0_cap1_tp1_edge
signals_total: 95
opened_signals: 76
mtm_pnl_pct: -1.8585%
mtm_mdd_pct: -1.9678%
```

Symbol-side diagnostic:

```text
split60_prior_symbol_side_min5_positive_oos_diagnostic
opened_signals: 15
best mtm_pnl_pct: +0.3598%

split70_prior_symbol_side_min5_positive_oos_diagnostic
opened_signals: 11
best mtm_pnl_pct: +0.2604%
```

## Interpretation

- `prior_symbol_min5_positive` improves strongly versus the OOS all-after controls, but it does not survive the stricter split70 as positive.
- Both OOS prior-symbol tests are below the 50-opened-trade promotion gate.
- Symbol-side OOS diagnostics are positive, but sample sizes are too small: 15 and 11 opened trades.
- DCA did not help these OOS filters; base no-DCA TP1 remains the best configuration.

## Decision

Promotion remains closed.

The next useful research step is not live/paper-live and not a larger DCA grid. It is either:

1. Increase sample size with more historical static Telegram signals, then rerun the same prior-only OOS filters.
2. Build a stricter causal symbol-state filter with minimum 5 prior trades, positive cumulative prior PnL, and a time-decay/regime reset, then test against all-after controls.
3. Keep `all_49` and OOS all-after controls mandatory in every report.
