# Akela Basket Validation Latest Summary

Updated: 20260513T004441Z
Baseline config: `obw_platform/configs/V21_freedommoney_bingx_live_candidate_1m_1y.yaml`
Backtester: `obw_platform/backtester_dual_long_short_fast_pack_v2.py`
Raw artifacts: `_reports/akela_meta_short/basket_20260513T004441Z`
Limit bars: `full`

## Basket Result

- successful symbols: 4/4
- equal-weight terminal return approximation: 72.81%
- worst single-symbol MTM drawdown: -219.88%
- total margin-call events: 61
- best symbol: `MAXXING/USDT:USDT` return 183.80%
- worst symbol: `SUP/USDT:USDT` return -1.05%

## Per-Symbol Results

| symbol | status | return_mtm_% | mdd_mtm_% | trades | margin_calls | seconds |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| `IDOL/USDT:USDT` | ok | 44.19 | -37.75 | 10963 | 18 | 122.27 |
| `FREEDOMMONEY/USDT:USDT` | ok | 64.28 | -24.09 | 7583 | 0 | 30.73 |
| `MAXXING/USDT:USDT` | ok | 183.80 | -18.37 | 18197 | 8 | 50.04 |
| `SUP/USDT:USDT` | ok | -1.05 | -219.88 | 8588 | 35 | 65.01 |

## Interpretation

This is validation of the upper-layer basket idea using the existing V21 short-leg backtester.
It is not a live promotion and it does not modify production strategy YAMLs.
The basket still has tail-risk work because at least one symbol hit margin-call events.
