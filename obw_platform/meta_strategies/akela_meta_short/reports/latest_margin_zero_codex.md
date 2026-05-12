# Akela Margin-Zero Codex Report

Updated: 2026-05-12

## Objective

Find V21-style experimental parameter configurations for the first Akela basket candidates that remove margin-call events without changing exchange, fee, slippage, liquidation, margin, or backtest math.

## Best Current Candidates

All candidates below are experimental YAMLs under `obw_platform/meta_strategies/akela_meta_short/generated_configs/margin_zero/`.

| symbol | config | return_mtm_% | mdd_mtm_% | trades | margin_calls | bars_in_margin_call | terminal_unrealized_to_realized_ratio | raw log |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `IDOL/USDT:USDT` | `V21_idol_margin_zero_budget50.yaml` | 9.05 | -10.40 | 2430 | 0 | 0 | -0.4943 | `_reports/akela_meta_short/margin_zero_codex_loop/idol_budget50_full.log` |
| `MAXXING/USDT:USDT` | `V21_maxxing_margin_zero_budget50.yaml` | 26.23 | -11.60 | 1459 | 0 | 0 | -0.3543 | `_reports/akela_meta_short/margin_zero_codex_loop/maxxing_budget50_full.log` |
| `SUP/USDT:USDT` | `V21_sup_margin_zero_budget50.yaml` | 5.34 | -49.94 | 1321 | 0 | 0 | -0.6659 | `_reports/akela_meta_short/margin_zero_codex_loop/sup_budget50_full.log` |

`FREEDOMMONEY/USDT:USDT` already had zero margin calls in the latest baseline basket with `V21_freedommoney_bingx_live_candidate_1m_1y.yaml`: return 64.28%, MDD -24.09%, trades 7583, margin calls 0.

## Baseline Comparison

| symbol | baseline return_mtm_% | baseline mdd_mtm_% | baseline margin_calls | margin-zero return_mtm_% | margin-zero mdd_mtm_% | margin-zero margin_calls |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `IDOL/USDT:USDT` | 44.19 | -37.75 | 18 | 9.05 | -10.40 | 0 |
| `MAXXING/USDT:USDT` | 183.80 | -18.37 | 8 | 26.23 | -11.60 | 0 |
| `SUP/USDT:USDT` | -1.05 | -219.88 | 35 | 5.34 | -49.94 | 0 |

## What Changed

The useful hypothesis was that `maxLongInvestPct` and `maxShortInvestPct` do not cap the full DCA cycle budget. In `strategies/cryptomine_pack_dual_full.py`, DCA checks against `equityForSizingUSDT`, so the working margin-zero variant lowers `equityForSizingUSDT` to 50 per side and reduces fill density:

- `equityForSizingUSDT: 50` for long and short
- `maxFillsPerBar: 2`
- `maxOrdersPer3Min: 4`
- wider spacing: long `linearDropPercent: 0.08`, short `linearRisePercent: 0.3`
- reduced adaptive entry sizing and later DCA multipliers

No portfolio, fee, slippage, liquidation, margin, exchange, or backtester math was changed.

## Attempts

| config | symbol/scope | limit | result | reason |
| --- | --- | ---: | --- | --- |
| baseline `V21_freedommoney_bingx_live_candidate_1m_1y.yaml` | `SUP/USDT:USDT` | 20000 | 23 margin calls | Reproduced the risk problem on a short slice. |
| baseline `V21_maxxing_bingx_live_candidate_1m_1y.yaml` | `SUP/USDT:USDT` | 20000 | 29 margin calls | Not safer for SUP. |
| `V21_sup_margin_zero_cap075.yaml` | `SUP/USDT:USDT` | 20000 | 18 margin calls | Adaptive max-invest caps alone did not cap DCA cycle exposure. |
| `V21_sup_margin_zero_cap050.yaml` | `SUP/USDT:USDT` | 20000 | 15 margin calls | Improved but still failed margin-zero target. |
| `V21_sup_margin_zero_long035_short075.yaml` | `SUP/USDT:USDT` | 20000 | 22 margin calls | Improved terminal MTM but drawdown still crossed margin threshold. |
| `V21_sup_margin_zero_risk_exit_budget50.yaml` | `SUP/USDT:USDT` | 20000 | 6 margin calls | Stale/contrary exits realized losses but did not fully prevent threshold crossings. |
| `V21_sup_margin_zero_budget25.yaml` | `SUP/USDT:USDT` | full | 0 margin calls, +1.31% MTM | Valid but too conservative versus budget50. |
| `V21_sup_margin_zero_budget50.yaml` | `SUP/USDT:USDT` | full | 0 margin calls, +5.34% MTM | Best SUP candidate from this cycle. |
| `V21_maxxing_margin_zero_budget50.yaml` | `MAXXING/USDT:USDT` | full | 0 margin calls, +26.23% MTM | Confirmed margin-zero candidate. |
| `V21_idol_margin_zero_budget50.yaml` | `IDOL/USDT:USDT` | full | 0 margin calls, +9.05% MTM | Confirmed margin-zero candidate. |

## Exact Full-Year Commands

```bash
python3 obw_platform/backtester_dual_long_short_fast_pack_v2.py --cfg obw_platform/meta_strategies/akela_meta_short/generated_configs/margin_zero/V21_sup_margin_zero_budget50.yaml --npz DB/akela_meta_short_1m_1y_sup_bingx.npz --symbol SUP/USDT:USDT
python3 obw_platform/backtester_dual_long_short_fast_pack_v2.py --cfg obw_platform/meta_strategies/akela_meta_short/generated_configs/margin_zero/V21_maxxing_margin_zero_budget50.yaml --npz DB/fast_cache_1m_maxxing_1y_bingx.npz --symbol MAXXING/USDT:USDT
python3 obw_platform/backtester_dual_long_short_fast_pack_v2.py --cfg obw_platform/meta_strategies/akela_meta_short/generated_configs/margin_zero/V21_idol_margin_zero_budget50.yaml --npz DB/akela_meta_short_1m_1y_idol_bingx.npz --symbol IDOL/USDT:USDT
```

## Next Action

Run an equal-weight basket validation using the three margin-zero generated configs plus the existing FREEDOMMONEY baseline-zero config, then decide whether to tune `budget50` upward carefully for SUP only.
