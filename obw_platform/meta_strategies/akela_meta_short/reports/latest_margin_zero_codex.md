# Akela Margin-Zero Codex Report

Updated: 2026-05-13T00:28:49Z

## Objective

Find V21-style experimental parameter configurations for the first Akela basket candidates that remove margin-call events without changing exchange, fee, slippage, liquidation, margin, or backtest math.

## Best Current Candidates

All generated candidates below are experimental YAMLs under `obw_platform/meta_strategies/akela_meta_short/generated_configs/margin_zero/`. `FREEDOMMONEY/USDT:USDT` keeps the existing production candidate because it was already zero-margin in the baseline basket.

| symbol | config | return_mtm_% | mdd_mtm_% | trades | margin_calls | bars_in_margin_call | terminal_unrealized_to_realized_ratio | raw log |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `IDOL/USDT:USDT` | `V21_idol_margin_zero_budget125.yaml` | 34.70 | -13.40 | 5848 | 0 | 0 | -0.2055 | `_reports/akela_meta_short/margin_zero_codex_loop/idol_budget125_full.log` |
| `FREEDOMMONEY/USDT:USDT` | `V21_freedommoney_bingx_live_candidate_1m_1y.yaml` | 64.28 | -24.09 | 7583 | 0 | 0 | -0.4130 | `_reports/akela_meta_short/margin_zero_codex_loop/freedommoney_baseline_full.log` |
| `MAXXING/USDT:USDT` | `V21_maxxing_margin_zero_budget125.yaml` | 104.68 | -21.17 | 4469 | 0 | 0 | -0.2273 | `_reports/akela_meta_short/margin_zero_codex_loop/maxxing_budget125_full.log` |
| `SUP/USDT:USDT` | `V21_sup_margin_zero_budget30_fast_exit.yaml` | 2.87 | -25.05 | 879 | 0 | 0 | -0.6742 | `_reports/akela_meta_short/margin_zero_codex_loop/sup_budget30_fast_exit_full.log` |

## Four-Symbol Basket Validation

| metric | value |
| --- | ---: |
| symbols | 4 |
| equal-weight terminal return approximation | 51.63% |
| worst single-symbol MTM drawdown | -25.05% |
| total trades | 18779 |
| total margin-call events | 0 |
| total bars in margin call | 0 |

## Current Cycle

The first-basket risk-cleanup target is still satisfied: all four selected configs have `margin_call_events_total = 0` and `bars_in_margin_call = 0` on full-year runs.

Selection note: `V21_sup_margin_zero_budget30_fast_exit.yaml` improves SUP full-year return from 1.90% to 2.87%, slightly improves MDD from -25.15% to -25.05%, and improves terminal unrealized/realized ratio from -0.7934 to -0.6742 versus plain budget30. Budget20 remains a valid lower-drawdown fallback, but fast-exit budget30 is the better current risk-cleanup balance.

Gap-test note: `V21_sup_margin_zero_budget45.yaml` had the best return in the new gap test at 5.61% with zero margin calls, but its full-year MDD was -45.45%. Budgets 35 and 40 also stayed zero-margin, but drew down -48.87% and -54.22%. This confirms that simply raising the SUP sizing budget recovers return while giving up too much drawdown for the immediate risk-cleanup target.

Near-budget note: `V21_sup_margin_zero_budget32.yaml` improved SUP full-year return to 2.65% with zero margin calls, but MDD was -30.41%. That makes it a useful secondary if a slightly deeper drawdown is acceptable, but it does not replace budget30 as the primary risk-cleanup pick. `V21_sup_margin_zero_budget31.yaml` also stayed zero-margin, but had slightly worse MDD (-30.46%) and lower return (2.35%) than budget32.

Tuner note: a 180-second 20k-bar tuner pass from `V21_sup_margin_zero_budget30.yaml` improved the slice to 4.91% return, -19.02% MDD, and zero margin calls. Its full-year confirmation stayed zero-margin, but returned 1.86% with -25.38% MDD, slightly worse than budget30's 1.90% and -25.15%. It is rejected as a primary candidate and was not promoted into `generated_configs`.

MAXXING best remains `V21_maxxing_margin_zero_budget125.yaml`; the previous short-cap sweep did not improve its full-year risk/return balance.

New SUP ladder note: the rejected higher-return SUP ladder variants had already shown that more active short-side ladders can lift return, but their full-year MDD rose near -50% when paired with larger sizing budgets. This cycle isolated that idea inside the budget30 sizing envelope. Both new 20k-bar tests stayed zero-margin, but neither earned full-year promotion: the combined long/short ladder variant only improved the slice by 0.06 return points while worsening MDD, and the short-only ladder variant underperformed budget30.

Fast-exit confirmation note: `V21_sup_margin_zero_budget30_fast_exit.yaml` looked only marginally better than plain budget30 on the 20k slice, but the full-year run confirmed a useful primary upgrade with no margin calls, slightly lower MDD, and materially better terminal unrealized exposure.

Latest follow-up note: transplanting the SUP fast-exit profile into IDOL/MAXXING budget125 stayed zero-margin on 20k slices, but worsened risk-adjusted performance. IDOL budget150 was reconstructed from the prior temporary screen and full-year confirmed; it also stayed zero-margin, but underperformed IDOL budget125 on return, drawdown, and terminal unrealized exposure. The selected basket is unchanged.

## Current Cycle Attempts

| config | limit | return_mtm_% | mdd_mtm_% | trades | margin_calls | bars_in_margin_call | tail ratio | result | raw log |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `V21_idol_margin_zero_budget125_fast_exit.yaml` | 20000 | -0.71 | -2.10 | 260 | 0 | 0 | -3.5281 | Rejected before full-year: stayed zero-margin, but worsened return, MDD, and terminal unrealized ratio versus IDOL budget125. | `_reports/akela_meta_short/margin_zero_codex_loop/idol_budget125_fast_exit_20k.log` |
| `V21_maxxing_margin_zero_budget125_fast_exit.yaml` | 20000 | 9.06 | -15.03 | 1498 | 0 | 0 | -0.7675 | Rejected before full-year: stayed zero-margin, but cut return from 23.10% to 9.06% and worsened MDD from -8.31% to -15.03% versus MAXXING budget125. | `_reports/akela_meta_short/margin_zero_codex_loop/maxxing_budget125_fast_exit_20k.log` |
| `V21_idol_margin_zero_budget150.yaml` | 20000 | -0.28 | -1.85 | 350 | 0 | 0 | -1.4251 | Passed 20k recheck and matched the prior temporary screen exactly; promoted only to full-year confirmation. | `_reports/akela_meta_short/margin_zero_codex_loop/idol_budget150_recheck_20k.log` |
| `V21_idol_margin_zero_budget150.yaml` | full | 29.43 | -15.81 | 6583 | 0 | 0 | -0.3544 | Rejected after full-year: zero-margin, but worse than IDOL budget125's 34.70% return, -13.40% MDD, and -0.2055 tail ratio. | `_reports/akela_meta_short/margin_zero_codex_loop/idol_budget150_full.log` |

## Baseline Comparison

| symbol | baseline return_mtm_% | baseline mdd_mtm_% | baseline margin_calls | margin-zero return_mtm_% | margin-zero mdd_mtm_% | margin-zero margin_calls |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `IDOL/USDT:USDT` | 44.19 | -37.75 | 18 | 34.70 | -13.40 | 0 |
| `FREEDOMMONEY/USDT:USDT` | 64.28 | -24.09 | 0 | 64.28 | -24.09 | 0 |
| `MAXXING/USDT:USDT` | 183.80 | -18.37 | 8 | 104.68 | -21.17 | 0 |
| `SUP/USDT:USDT` | -1.05 | -219.88 | 35 | 2.87 | -25.05 | 0 |

## Exact Full-Year Commands

```bash
python3 obw_platform/backtester_dual_long_short_fast_pack_v2.py --cfg obw_platform/meta_strategies/akela_meta_short/generated_configs/margin_zero/V21_idol_margin_zero_budget125.yaml --npz DB/akela_meta_short_1m_1y_idol_bingx.npz --symbol IDOL/USDT:USDT
```
```bash
python3 obw_platform/backtester_dual_long_short_fast_pack_v2.py --cfg obw_platform/configs/V21_freedommoney_bingx_live_candidate_1m_1y.yaml --npz DB/fast_cache_1m_freedommoney_1y_bingx.npz --symbol FREEDOMMONEY/USDT:USDT
```
```bash
python3 obw_platform/backtester_dual_long_short_fast_pack_v2.py --cfg obw_platform/meta_strategies/akela_meta_short/generated_configs/margin_zero/V21_maxxing_margin_zero_budget125.yaml --npz DB/fast_cache_1m_maxxing_1y_bingx.npz --symbol MAXXING/USDT:USDT
```
```bash
python3 obw_platform/backtester_dual_long_short_fast_pack_v2.py --cfg obw_platform/meta_strategies/akela_meta_short/generated_configs/margin_zero/V21_sup_margin_zero_budget30_fast_exit.yaml --npz DB/akela_meta_short_1m_1y_sup_bingx.npz --symbol SUP/USDT:USDT
```

## Next Action

First-basket margin-call cleanup remains satisfied with 0 total margin-call events using SUP budget30_fast_exit. The SUP fast-exit transplant and IDOL budget150 are rejected, so the next useful search is either a narrow SUP budget30 fast-exit refinement or a different MAXXING exit/spacing hypothesis.
