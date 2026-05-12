# Akela Margin-Zero Codex Report

Updated: 2026-05-12T23:24:00Z

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

Audit pass after the MAXXING short-cap sweep. The first-basket risk-cleanup target is satisfied: all four selected configs have `margin_call_events_total = 0` and `bars_in_margin_call = 0` on full-year runs.

Selection note: SUP has higher-return zero-margin variants, but they raise full-year MDD sharply. `V21_sup_margin_zero_budget20.yaml` remains the preferred cleanup candidate because this lane prioritizes zero margin calls and drawdown containment over return recovery.

MAXXING tail-drag cleanup tested short-leg sizing/cap reductions around the current `V21_maxxing_margin_zero_budget125.yaml` champion. All screened variants kept margin calls at 0, but none improved the full-year risk/return balance: `shortcap100` reduced return from 104.68% to 90.06%, left MDD effectively unchanged, and worsened terminal unrealized ratio from -0.2273 to -0.2526. MAXXING best remains `budget125`.

## Current Cycle Attempts

| config | limit | return_mtm_% | mdd_mtm_% | trades | margin_calls | bars_in_margin_call | tail ratio | result | raw log |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `V21_maxxing_margin_zero_shortcap100.yaml` | 20000 | 21.08 | -7.80 | 1244 | 0 | 0 | -0.3687 | Clean screen, but lower return and worse tail ratio than budget125 20k. | `_reports/akela_meta_short/margin_zero_codex_loop/maxxing_shortcap100_20k.log` |
| `V21_maxxing_margin_zero_shortcap085.yaml` | 20000 | 21.08 | -7.80 | 1244 | 0 | 0 | -0.3687 | Same slice result as shortcap100; cap did not bind enough to justify full-year confirmation. | `_reports/akela_meta_short/margin_zero_codex_loop/maxxing_shortcap085_20k.log` |
| `V21_maxxing_margin_zero_short_ladder_soft.yaml` | 20000 | 0.54 | -19.52 | 1311 | 0 | 0 | -0.9839 | Rejected: softer short ladder worsened terminal drag sharply. | `_reports/akela_meta_short/margin_zero_codex_loop/maxxing_short_ladder_soft_20k.log` |
| `V21_maxxing_margin_zero_shortcap100.yaml` | full | 90.06 | -21.21 | 3987 | 0 | 0 | -0.2526 | Rejected versus budget125: lower return, no drawdown improvement, worse terminal unrealized ratio. | `_reports/akela_meta_short/margin_zero_codex_loop/maxxing_shortcap100_full.log` |

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

The first-basket margin-call cleanup remains satisfied with 0 total margin-call events. Next useful work is a SUP return-recovery search constrained to budget20-level risk: keep full-year `margin_call_events_total = 0`, `bars_in_margin_call = 0`, and target MDD no worse than roughly -30% before considering higher-return variants. A secondary path is a targeted MAXXING short exit/deleverage search that improves terminal unrealized drag without giving up the existing zero-margin result.
