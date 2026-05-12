# Akela Margin-Zero Codex Report

Updated: 2026-05-12T23:59:00Z

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

SUP budget35/40/45 gap test found additional full-year zero-margin variants, but none replaced budget30 for risk cleanup. The first-basket risk-cleanup target is still satisfied: all four selected configs have `margin_call_events_total = 0` and `bars_in_margin_call = 0` on full-year runs.

Selection note: `V21_sup_margin_zero_budget30.yaml` improves SUP full-year return versus budget20 and budget25 while staying inside the roughly -30% SUP risk envelope. Budget20 remains a valid lower-drawdown fallback, but budget30 is the better current risk-cleanup balance.

Gap-test note: `V21_sup_margin_zero_budget45.yaml` had the best return in the new gap test at 5.61% with zero margin calls, but its full-year MDD was -45.45%. Budgets 35 and 40 also stayed zero-margin, but drew down -48.87% and -54.22%. This confirms that simply raising the SUP sizing budget recovers return while giving up too much drawdown for the immediate risk-cleanup target.

MAXXING best remains `V21_maxxing_margin_zero_budget125.yaml`; the previous short-cap sweep did not improve its full-year risk/return balance.

## Current Cycle Attempts

| config | limit | return_mtm_% | mdd_mtm_% | trades | margin_calls | bars_in_margin_call | tail ratio | result | raw log |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `V21_sup_margin_zero_budget35.yaml` | 20000 | 7.41 | -17.35 | 241 | 0 | 0 | -0.5189 | Passed 20k zero-margin filter; full-year confirmation required. | `_reports/akela_meta_short/margin_zero_codex_loop/sup_budget35_20k.log` |
| `V21_sup_margin_zero_budget40.yaml` | 20000 | 5.25 | -20.18 | 239 | 0 | 0 | -0.6470 | Passed 20k zero-margin filter; full-year confirmation required. | `_reports/akela_meta_short/margin_zero_codex_loop/sup_budget40_20k.log` |
| `V21_sup_margin_zero_budget45.yaml` | 20000 | 16.25 | -24.70 | 285 | 0 | 0 | -0.3003 | Passed 20k zero-margin filter; best slice return in this gap batch. | `_reports/akela_meta_short/margin_zero_codex_loop/sup_budget45_20k.log` |
| `V21_sup_margin_zero_budget35.yaml` | full | 3.36 | -48.87 | 886 | 0 | 0 | -0.6831 | Rejected as primary: zero-margin but drawdown is materially worse than budget30. | `_reports/akela_meta_short/margin_zero_codex_loop/sup_budget35_full.log` |
| `V21_sup_margin_zero_budget40.yaml` | full | 5.53 | -54.22 | 997 | 0 | 0 | -0.5267 | Rejected as primary: higher return, but drawdown is outside the risk-cleanup target. | `_reports/akela_meta_short/margin_zero_codex_loop/sup_budget40_full.log` |
| `V21_sup_margin_zero_budget45.yaml` | full | 5.61 | -45.45 | 1150 | 0 | 0 | -0.5433 | Rejected as primary: best gap-test return, but budget30 remains the cleaner risk candidate. | `_reports/akela_meta_short/margin_zero_codex_loop/sup_budget45_full.log` |

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

First-basket margin-call cleanup remains satisfied with 0 total margin-call events using SUP budget30. The next useful SUP search should target budget30-or-better return while keeping MDD near the -25% to -30% band; budget35/40/45 show that simply raising sizing budget recovers return but gives up too much drawdown.
