# Akela Basket Validation Latest Summary

Updated: 20260512T203841Z
Baseline config: `obw_platform/configs/V21_freedommoney_bingx_live_candidate_1m_1y.yaml`
Backtester: `obw_platform/backtester_dual_long_short_fast_pack_v2.py`
Raw artifacts: `_reports/akela_meta_short/basket_20260512T203841Z`
Limit bars: `5000`

## Basket Result

- successful symbols: 4/4
- equal-weight terminal return approximation: -0.32%
- worst single-symbol MTM drawdown: -21.79%
- total margin-call events: 32
- best symbol: `FREEDOMMONEY/USDT:USDT` return 1.01%
- worst symbol: `SUP/USDT:USDT` return -2.35%

## Per-Symbol Results

| symbol | status | return_mtm_% | mdd_mtm_% | trades | margin_calls | seconds |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| `IDOL/USDT:USDT` | ok | -0.32 | -1.78 | 244 | 0 | 1.86 |
| `FREEDOMMONEY/USDT:USDT` | ok | 1.01 | -5.21 | 685 | 0 | 2.12 |
| `MAXXING/USDT:USDT` | ok | 0.40 | -21.79 | 1229 | 32 | 2.64 |
| `SUP/USDT:USDT` | ok | -2.35 | -11.99 | 1107 | 0 | 3.03 |

## Interpretation

This is validation of the upper-layer basket idea using the existing V21 short-leg backtester.
It is not a live promotion and it does not modify production strategy YAMLs.
The basket still has tail-risk work because at least one symbol hit margin-call events.
