# Akela Margin-Zero Codex Report

Updated: 2026-05-12T22:50:16Z

## Objective

Find V21-style experimental parameter configurations for the first Akela basket candidates that remove margin-call events without changing exchange, fee, slippage, liquidation, margin, or backtest math.

## Best Current Candidates

All generated candidates below are experimental YAMLs under `obw_platform/meta_strategies/akela_meta_short/generated_configs/margin_zero/`. `FREEDOMMONEY/USDT:USDT` keeps the existing production candidate because it was already zero-margin in the baseline basket.

| symbol | config | return_mtm_% | mdd_mtm_% | trades | margin_calls | bars_in_margin_call | terminal_unrealized_to_realized_ratio | raw log |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `IDOL/USDT:USDT` | `V21_idol_margin_zero_budget125.yaml` | 34.70 | -13.40 | 5848 | 0 | 0 | -0.2055 | `_reports/akela_meta_short/margin_zero_codex_loop/idol_budget125_full.log` |
| `FREEDOMMONEY/USDT:USDT` | `V21_freedommoney_bingx_live_candidate_1m_1y.yaml` | 64.28 | -24.09 | 7583 | 0 | 0 | -0.4130 | `_reports/akela_meta_short/margin_zero_codex_loop/freedommoney_baseline_full.log` |
| `MAXXING/USDT:USDT` | `V21_maxxing_margin_zero_budget125.yaml` | 104.68 | -21.17 | 4469 | 0 | 0 | -0.2273 | `_reports/akela_meta_short/margin_zero_codex_loop/maxxing_budget125_full.log` |
| `SUP/USDT:USDT` | `V21_sup_margin_zero_short_ladder_soft.yaml` | 9.04 | -48.02 | 1329 | 0 | 0 | -0.3140 | `_reports/akela_meta_short/margin_zero_codex_loop/sup_short_ladder_soft_full.log` |

## Four-Symbol Basket Validation

| metric | value |
| --- | ---: |
| symbols | 4 |
| equal-weight terminal return approximation | 53.18% |
| worst single-symbol MTM drawdown | -48.02% |
| total trades | 19229 |
| total margin-call events | 0 |
| total bars in margin call | 0 |

## Current Cycle

IDOL second-pass cleanup tested budget lifts from the existing `V21_idol_margin_zero_budget50.yaml`. The useful result is `V21_idol_margin_zero_budget125.yaml`: full-year return improved from 9.05% to 34.70%, margin calls stayed at 0, bars in margin call stayed at 0, MDD moved from -10.40% to -13.40%, and terminal unrealized/realized ratio improved from -0.4943 to -0.2055.

Budget175 also passed full-year margin-zero confirmation, but it was rejected because it lowered return to 31.10%, worsened MDD to -17.45%, and increased terminal unrealized drag to -40.43 USDT. Temporary budget75, budget100, budget150, and budget175 YAMLs were removed after logging the raw results.

## Current Cycle Attempts

| config | limit | return_mtm_% | mdd_mtm_% | trades | margin_calls | bars_in_margin_call | tail ratio | result | raw log |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `V21_idol_margin_zero_budget50.yaml` | 20000 | -0.58 | -1.24 | 139 | 0 | 0 | -2.5031 | 20k IDOL budget-lift screen; budget125 and budget175 earned full-year confirmation, others were not promoted. | `_reports/akela_meta_short/margin_zero_codex_loop/idol_budget50_20k.log` |
| `V21_idol_margin_zero_budget75.yaml` | 20000 | -0.62 | -1.49 | 175 | 0 | 0 | -3.3247 | 20k IDOL budget-lift screen; budget125 and budget175 earned full-year confirmation, others were not promoted. | `_reports/akela_meta_short/margin_zero_codex_loop/idol_budget75_20k.log` |
| `V21_idol_margin_zero_budget100.yaml` | 20000 | -0.63 | -2.05 | 212 | 0 | 0 | -3.3106 | 20k IDOL budget-lift screen; budget125 and budget175 earned full-year confirmation, others were not promoted. | `_reports/akela_meta_short/margin_zero_codex_loop/idol_budget100_20k.log` |
| `V21_idol_margin_zero_budget125.yaml` | 20000 | -0.55 | -2.08 | 247 | 0 | 0 | -2.3165 | 20k IDOL budget-lift screen; budget125 and budget175 earned full-year confirmation, others were not promoted. | `_reports/akela_meta_short/margin_zero_codex_loop/idol_budget125_20k.log` |
| `V21_idol_margin_zero_budget150.yaml` | 20000 | -0.28 | -1.85 | 350 | 0 | 0 | -1.4251 | 20k IDOL budget-lift screen; budget125 and budget175 earned full-year confirmation, others were not promoted. | `_reports/akela_meta_short/margin_zero_codex_loop/idol_budget150_20k.log` |
| `V21_idol_margin_zero_budget175.yaml` | 20000 | 0.01 | -1.86 | 403 | 0 | 0 | -0.9870 | 20k IDOL budget-lift screen; budget125 and budget175 earned full-year confirmation, others were not promoted. | `_reports/akela_meta_short/margin_zero_codex_loop/idol_budget175_20k.log` |
| `V21_idol_margin_zero_budget125.yaml` | full | 34.70 | -13.40 | 5848 | 0 | 0 | -0.2055 | Promoted as IDOL best. | `_reports/akela_meta_short/margin_zero_codex_loop/idol_budget125_full.log` |
| `V21_idol_margin_zero_budget175.yaml` | full | 31.10 | -17.45 | 7330 | 0 | 0 | -0.3940 | Rejected: zero-margin but worse return, MDD, and terminal unrealized drag than budget125. | `_reports/akela_meta_short/margin_zero_codex_loop/idol_budget175_full.log` |

## Baseline Comparison

| symbol | baseline return_mtm_% | baseline mdd_mtm_% | baseline margin_calls | margin-zero return_mtm_% | margin-zero mdd_mtm_% | margin-zero margin_calls |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `IDOL/USDT:USDT` | 44.19 | -37.75 | 18 | 34.70 | -13.40 | 0 |
| `FREEDOMMONEY/USDT:USDT` | 64.28 | -24.09 | 0 | 64.28 | -24.09 | 0 |
| `MAXXING/USDT:USDT` | 183.80 | -18.37 | 8 | 104.68 | -21.17 | 0 |
| `SUP/USDT:USDT` | -1.05 | -219.88 | 35 | 9.04 | -48.02 | 0 |

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
python3 obw_platform/backtester_dual_long_short_fast_pack_v2.py --cfg obw_platform/meta_strategies/akela_meta_short/generated_configs/margin_zero/V21_sup_margin_zero_short_ladder_soft.yaml --npz DB/akela_meta_short_1m_1y_sup_bingx.npz --symbol SUP/USDT:USDT
```

## Next Action

The first-basket margin-call cleanup remains satisfied with 0 total margin-call events. The next useful work is either a bounded SUP risk cleanup to reduce the -48.02% MDD without losing zero-margin status, or a MAXXING tail-drag cleanup around budget125.
