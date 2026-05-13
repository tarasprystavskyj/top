# Akela Margin-Zero Codex Report

Updated: 2026-05-13T00:03:00Z

## Objective

Find V21-style experimental parameter configurations for the first Akela basket candidates that remove margin-call events without changing exchange, fee, slippage, liquidation, margin, or backtest math.

## Best Current Candidates

All generated candidates below are experimental YAMLs under `obw_platform/meta_strategies/akela_meta_short/generated_configs/margin_zero/`. `FREEDOMMONEY/USDT:USDT` keeps the existing production candidate because it was already zero-margin in the baseline basket.

| symbol | config | return_mtm_% | mdd_mtm_% | trades | margin_calls | bars_in_margin_call | terminal_unrealized_to_realized_ratio | raw log |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `IDOL/USDT:USDT` | `V21_idol_margin_zero_budget125.yaml` | 34.70 | -13.40 | 5848 | 0 | 0 | -0.2055 | `_reports/akela_meta_short/margin_zero_codex_loop/idol_budget125_full.log` |
| `FREEDOMMONEY/USDT:USDT` | `V21_freedommoney_bingx_live_candidate_1m_1y.yaml` | 64.28 | -24.09 | 7583 | 0 | 0 | -0.4130 | `_reports/akela_meta_short/margin_zero_codex_loop/freedommoney_baseline_full.log` |
| `MAXXING/USDT:USDT` | `V21_maxxing_margin_zero_budget125.yaml` | 104.68 | -21.17 | 4469 | 0 | 0 | -0.2273 | `_reports/akela_meta_short/margin_zero_codex_loop/maxxing_budget125_full.log` |
| `SUP/USDT:USDT` | `V21_sup_margin_zero_budget30.yaml` | 1.90 | -25.15 | 845 | 0 | 0 | -0.7934 | `_reports/akela_meta_short/margin_zero_codex_loop/sup_budget30_recheck_full.log` |

## Four-Symbol Basket Validation

| metric | value |
| --- | ---: |
| symbols | 4 |
| equal-weight terminal return approximation | 51.39% |
| worst single-symbol MTM drawdown | -25.15% |
| total trades | 18745 |
| total margin-call events | 0 |
| total bars in margin call | 0 |

## Current Cycle

SUP budget30 was used as a tuner seed for a short, 20k-bar exit/spacing search. The first-basket risk-cleanup target is still satisfied: all four selected configs have `margin_call_events_total = 0` and `bars_in_margin_call = 0` on full-year runs.

Selection note: `V21_sup_margin_zero_budget30.yaml` improves SUP full-year return versus budget20 and budget25 while staying inside the roughly -30% SUP risk envelope. Budget20 remains a valid lower-drawdown fallback, but budget30 is the better current risk-cleanup balance.

Gap-test note: `V21_sup_margin_zero_budget45.yaml` had the best return in the new gap test at 5.61% with zero margin calls, but its full-year MDD was -45.45%. Budgets 35 and 40 also stayed zero-margin, but drew down -48.87% and -54.22%. This confirms that simply raising the SUP sizing budget recovers return while giving up too much drawdown for the immediate risk-cleanup target.

Near-budget note: `V21_sup_margin_zero_budget32.yaml` improved SUP full-year return to 2.65% with zero margin calls, but MDD was -30.41%. That makes it a useful secondary if a slightly deeper drawdown is acceptable, but it does not replace budget30 as the primary risk-cleanup pick. `V21_sup_margin_zero_budget31.yaml` also stayed zero-margin, but had slightly worse MDD (-30.46%) and lower return (2.35%) than budget32.

Tuner note: a 180-second 20k-bar tuner pass from `V21_sup_margin_zero_budget30.yaml` improved the slice to 4.91% return, -19.02% MDD, and zero margin calls. Its full-year confirmation stayed zero-margin, but returned 1.86% with -25.38% MDD, slightly worse than budget30's 1.90% and -25.15%. It is rejected as a primary candidate and was not promoted into `generated_configs`.

MAXXING best remains `V21_maxxing_margin_zero_budget125.yaml`; the previous short-cap sweep did not improve its full-year risk/return balance.

## Current Cycle Attempts

| config | limit | return_mtm_% | mdd_mtm_% | trades | margin_calls | bars_in_margin_call | tail ratio | result | raw log |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `V21_sup_margin_zero_budget30.yaml` tuner final | 20000 | 4.91 | -19.02 | 214 | 0 | 0 | -0.6217 | Passed the 20k zero-margin filter and improved the slice versus budget30. Full-year confirmation required. | `_reports/akela_meta_short/margin_zero_codex_loop/sup_budget30_tuner_20k_20260512.log` |
| `V21_sup_margin_zero_budget30.yaml` tuner final | full | 1.86 | -25.38 | 1015 | 0 | 0 | -0.7968 | Rejected as primary: full-year zero-margin, but slightly worse return and drawdown than budget30. | `_reports/akela_meta_short/margin_zero_codex_loop/sup_budget30_tuner_exit_soft_full.log` |
| `V21_sup_margin_zero_budget32.yaml` | 20000 | 4.75 | -17.47 | 230 | 0 | 0 | -0.6229 | Passed 20k zero-margin filter; best slice return in this near-budget30 batch. | `_reports/akela_meta_short/margin_zero_codex_loop/sup_budget32_20k.log` |
| `V21_sup_margin_zero_long30_short35.yaml` | 20000 | 4.05 | -18.93 | 227 | 0 | 0 | -0.6637 | Passed 20k, but did not beat budget32 on slice return or drawdown. | `_reports/akela_meta_short/margin_zero_codex_loop/sup_long30_short35_20k.log` |
| `V21_sup_margin_zero_budget30_fast_exit.yaml` | 20000 | 3.91 | -19.73 | 216 | 0 | 0 | -0.6734 | Passed 20k, but faster exits did not improve this slice. | `_reports/akela_meta_short/margin_zero_codex_loop/sup_budget30_fast_exit_20k.log` |
| `V21_sup_margin_zero_budget32.yaml` | full | 2.65 | -30.41 | 851 | 0 | 0 | -0.7280 | Secondary candidate: better return than budget30, but MDD is just outside the preferred band. | `_reports/akela_meta_short/margin_zero_codex_loop/sup_budget32_full.log` |
| `V21_sup_margin_zero_budget31.yaml` | 20000 | 4.33 | -17.49 | 223 | 0 | 0 | -0.6444 | Passed 20k as a tighter midpoint after budget32 full-year drawdown came in slightly high. | `_reports/akela_meta_short/margin_zero_codex_loop/sup_budget31_20k.log` |
| `V21_sup_margin_zero_budget31.yaml` | full | 2.35 | -30.46 | 830 | 0 | 0 | -0.7512 | Rejected as primary: lower return and slightly worse MDD than budget32. | `_reports/akela_meta_short/margin_zero_codex_loop/sup_budget31_full.log` |

## Baseline Comparison

| symbol | baseline return_mtm_% | baseline mdd_mtm_% | baseline margin_calls | margin-zero return_mtm_% | margin-zero mdd_mtm_% | margin-zero margin_calls |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `IDOL/USDT:USDT` | 44.19 | -37.75 | 18 | 34.70 | -13.40 | 0 |
| `FREEDOMMONEY/USDT:USDT` | 64.28 | -24.09 | 0 | 64.28 | -24.09 | 0 |
| `MAXXING/USDT:USDT` | 183.80 | -18.37 | 8 | 104.68 | -21.17 | 0 |
| `SUP/USDT:USDT` | -1.05 | -219.88 | 35 | 1.90 | -25.15 | 0 |

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
python3 obw_platform/backtester_dual_long_short_fast_pack_v2.py --cfg obw_platform/meta_strategies/akela_meta_short/generated_configs/margin_zero/V21_sup_margin_zero_budget30.yaml --npz DB/akela_meta_short_1m_1y_sup_bingx.npz --symbol SUP/USDT:USDT
```

## Next Action

First-basket margin-call cleanup remains satisfied with 0 total margin-call events using SUP budget30. Budget32 is a secondary if a slightly higher -30.41% drawdown is acceptable. The next useful SUP search should improve return without increasing sizing budget, for example exit/spacing changes around budget30.
