# Akela Margin-Zero Codex Report

Updated: 2026-05-12T23:32:00Z

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

SUP budget30 recheck promoted a modest return-recovery candidate. The first-basket risk-cleanup target is still satisfied: all four selected configs have `margin_call_events_total = 0` and `bars_in_margin_call = 0` on full-year runs.

Selection note: `V21_sup_margin_zero_budget30.yaml` improves SUP full-year return versus budget20 and budget25 while staying inside the roughly -30% SUP risk envelope. Budget20 remains a valid lower-drawdown fallback, but budget30 is the better current risk-cleanup balance.

MAXXING best remains `V21_maxxing_margin_zero_budget125.yaml`; the previous short-cap sweep did not improve its full-year risk/return balance.

## Current Cycle Attempts

| config | limit | return_mtm_% | mdd_mtm_% | trades | margin_calls | bars_in_margin_call | tail ratio | result | raw log |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `V21_sup_margin_zero_budget30.yaml` | full | 1.90 | -25.15 | 845 | 0 | 0 | -0.7934 | Promoted: better SUP return than budget20 and budget25 while keeping MDD inside the roughly -30% risk envelope. | `_reports/akela_meta_short/margin_zero_codex_loop/sup_budget30_recheck_full.log` |

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

First-basket margin-call cleanup remains satisfied with 0 total margin-call events using SUP budget30. Next useful work is either a stricter SUP search for budget30-or-better return with MDD closer to budget20, or a targeted MAXXING terminal-unrealized cleanup that preserves the budget125 zero-margin result.
