# Akela Margin-Zero Codex Report

Updated: 2026-05-12T23:01:10Z

## Objective

Find V21-style experimental parameter configurations for the first Akela basket candidates that remove margin-call events without changing exchange, fee, slippage, liquidation, margin, or backtest math.

## Best Current Candidates

All generated candidates below are experimental YAMLs under `obw_platform/meta_strategies/akela_meta_short/generated_configs/margin_zero/`. `FREEDOMMONEY/USDT:USDT` keeps the existing production candidate because it was already zero-margin in the baseline basket.

| symbol | config | return_mtm_% | mdd_mtm_% | trades | margin_calls | bars_in_margin_call | terminal_unrealized_to_realized_ratio | raw log |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `IDOL/USDT:USDT` | `V21_idol_margin_zero_budget125.yaml` | 34.70 | -13.40 | 5848 | 0 | 0 | -0.2055 | `_reports/akela_meta_short/margin_zero_codex_loop/idol_budget125_full.log` |
| `FREEDOMMONEY/USDT:USDT` | `V21_freedommoney_bingx_live_candidate_1m_1y.yaml` | 64.28 | -24.09 | 7583 | 0 | 0 | -0.4130 | `_reports/akela_meta_short/margin_zero_codex_loop/freedommoney_baseline_full.log` |
| `MAXXING/USDT:USDT` | `V21_maxxing_margin_zero_budget125.yaml` | 104.68 | -21.17 | 4469 | 0 | 0 | -0.2273 | `_reports/akela_meta_short/margin_zero_codex_loop/maxxing_budget125_full.log` |
| `SUP/USDT:USDT` | `V21_sup_margin_zero_budget20.yaml` | 1.08 | -23.31 | 654 | 0 | 0 | -0.8609 | `_reports/akela_meta_short/margin_zero_codex_loop/sup_budget20_full.log` |

## Four-Symbol Basket Validation

| metric | value |
| --- | ---: |
| symbols | 4 |
| equal-weight terminal return approximation | 51.19% |
| worst single-symbol MTM drawdown | -24.09% |
| total trades | 18554 |
| total margin-call events | 0 |
| total bars in margin call | 0 |

## Current Cycle

SUP risk cleanup promoted V21_sup_margin_zero_budget20.yaml: full-year margin calls stayed at 0, MDD improved from -48.02% on short_ladder_soft to -23.31%, and basket worst drawdown moved to FREEDOMMONEY at -24.09%. Return fell from 9.04% to 1.08%, accepted because this cycle prioritized risk cleanup over maximum return.

## Current Cycle Attempts

| config | limit | return_mtm_% | mdd_mtm_% | trades | margin_calls | bars_in_margin_call | tail ratio | result | raw log |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `V21_sup_margin_zero_budget10.yaml` | 20000 | 0.61 | -23.93 | 80 | 0 | 0 | -0.9022 | SUP conservative sizing screen. | `_reports/akela_meta_short/margin_zero_codex_loop/sup_budget10_20k.log` |
| `V21_sup_margin_zero_budget10.yaml` | full | 0.70 | -26.33 | 423 | 0 | 0 | -0.9131 | Full-year SUP conservative sizing confirmation. Rejected versus budget20 on return/MDD balance. | `_reports/akela_meta_short/margin_zero_codex_loop/sup_budget10_full.log` |
| `V21_sup_margin_zero_budget15.yaml` | 20000 | 1.06 | -23.71 | 114 | 0 | 0 | -0.8640 | SUP conservative sizing screen. | `_reports/akela_meta_short/margin_zero_codex_loop/sup_budget15_20k.log` |
| `V21_sup_margin_zero_budget15.yaml` | full | 0.65 | -28.32 | 525 | 0 | 0 | -0.9222 | Full-year SUP conservative sizing confirmation. Rejected versus budget20 on return/MDD balance. | `_reports/akela_meta_short/margin_zero_codex_loop/sup_budget15_full.log` |
| `V21_sup_margin_zero_budget20.yaml` | 20000 | 2.32 | -13.74 | 134 | 0 | 0 | -0.7362 | SUP conservative sizing screen. | `_reports/akela_meta_short/margin_zero_codex_loop/sup_budget20_20k.log` |
| `V21_sup_margin_zero_budget20.yaml` | full | 1.08 | -23.31 | 654 | 0 | 0 | -0.8609 | Promoted as lower-risk SUP best: zero margin calls with materially lower MDD than short_ladder_soft. | `_reports/akela_meta_short/margin_zero_codex_loop/sup_budget20_full.log` |
| `V21_sup_margin_zero_budget30.yaml` | 20000 | 3.81 | -19.76 | 218 | 0 | 0 | -0.6795 | SUP conservative sizing screen. | `_reports/akela_meta_short/margin_zero_codex_loop/sup_budget30_20k.log` |
| `V21_sup_margin_zero_budget30.yaml` | full | 1.90 | -25.15 | 845 | 0 | 0 | -0.7934 | Full-year SUP conservative sizing confirmation. Rejected versus budget20 on return/MDD balance. | `_reports/akela_meta_short/margin_zero_codex_loop/sup_budget30_full.log` |
| `V21_sup_margin_zero_short_ladder_soft.yaml` | full | 9.04 | -48.02 | 1329 | 0 | 0 | -0.3140 | Demoted as SUP best because budget20 cuts full-year MDD from -48.02% to -23.31%, despite lower return. | `_reports/akela_meta_short/margin_zero_codex_loop/sup_short_ladder_soft_full.log` |

## Baseline Comparison

| symbol | baseline return_mtm_% | baseline mdd_mtm_% | baseline margin_calls | margin-zero return_mtm_% | margin-zero mdd_mtm_% | margin-zero margin_calls |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `IDOL/USDT:USDT` | 44.19 | -37.75 | 18 | 34.70 | -13.40 | 0 |
| `FREEDOMMONEY/USDT:USDT` | 64.28 | -24.09 | 0 | 64.28 | -24.09 | 0 |
| `MAXXING/USDT:USDT` | 183.80 | -18.37 | 8 | 104.68 | -21.17 | 0 |
| `SUP/USDT:USDT` | -1.05 | -219.88 | 35 | 1.08 | -23.31 | 0 |

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
python3 obw_platform/backtester_dual_long_short_fast_pack_v2.py --cfg obw_platform/meta_strategies/akela_meta_short/generated_configs/margin_zero/V21_sup_margin_zero_budget20.yaml --npz DB/akela_meta_short_1m_1y_sup_bingx.npz --symbol SUP/USDT:USDT
```

## Next Action

The first-basket margin-call cleanup remains satisfied with 0 total margin-call events. Next useful work is MAXXING tail-drag cleanup around budget125, or a SUP search that recovers return while preserving budget20-level drawdown.
