# Rolling Symbol-State Results

Date: 2026-05-16
Mode: paper-only research
Live readiness: false

## Objective

Test whether a causal rolling/regime-reset variant improves on the prior-only symbol filter.

Source:

`all_49__adds1__cap2x__tp1__wedge/telegram_dca_trades.csv`

## Selector Check

Best rolling selector on the trade stream:

```text
rolling180d_symbol_side_min3_positive
split60_after: selected=25 pnl=+0.5522% mdd=0.0000%
split70_after: selected=20 pnl=+0.4344% mdd=0.0000%
```

This uses only the last 180 days of prior trades for the same symbol+side and requires:

```text
prior trades in rolling window >= 3
rolling prior pnl > 0
```

## Runner Confirmation

The selector was converted into signal CSVs and rerun through `run_telegram_dca_mvp_npz.py`.

```text
split60_rolling180d_symbol_side_min3_positive_oos__base_adds0_cap1_tp1_edge
signals_total: 25
opened_signals: 25
mtm_pnl_pct: +0.5522%
mtm_mdd_pct: -0.6083%
mtm_to_mdd: 0.9078

split70_rolling180d_symbol_side_min3_positive_oos__base_adds0_cap1_tp1_edge
signals_total: 20
opened_signals: 20
mtm_pnl_pct: +0.4344%
mtm_mdd_pct: -0.6090%
mtm_to_mdd: 0.7133
```

DCA variant produced the same results because there were no DCA fills.

## Interpretation

- This is the strongest causal-looking selector found so far.
- It is positive on both time splits.
- It is still below the minimum sample gate: 25 and 20 opened trades.
- It cannot be promoted, but it is a concrete next research target.

## Next Gate

Promotion remains closed.

Next bounded step:

1. Increase sample size before any further tuning claim.
2. If no more history is available, test neighboring causal variants only:
   - rolling120d_symbol_side_min3_positive
   - rolling240d_symbol_side_min3_positive
   - rolling180d_symbol_side_min4_positive
3. Keep all-after controls visible.
4. Do not run broad full-universe DCA grids; DCA is not the source of edge here.
