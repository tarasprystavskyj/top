# Akela Yearly Champion Search

Updated: 20260512T133459Z
Run dir: `_reports/akela_meta_short/champion_20260512T133459Z`

## Objective

Find a new candidate champion for paper live using existing V21 backtester/tuner only.

## Guardrails

- Existing backtester only: `obw_platform/backtester_dual_long_short_fast_pack_v2.py`.
- Existing tuner only: `obw_platform/auto_tuner_dual_fast_pack.py`.
- Existing tuning plan only: `obw_platform/tuner_plans/tuner_plan_V21_live_candidates_1m_1y.py`.
- No live/deploy changes.
- No production YAML edits.
- No exchange, fee, slippage, liquidation, or backtest math changes.

## Yearly Backtest Matrix

| symbol | config | status | log |
| --- | --- | --- | --- |
| `IDOL/USDT:USDT` | `V21_freedommoney_bingx_live_candidate_1m_1y` | ok | `_reports/akela_meta_short/champion_20260512T133459Z/backtests/IDOL/V21_freedommoney_bingx_live_candidate_1m_1y/backtest.log` |
| `IDOL/USDT:USDT` | `V21_maxxing_bingx_live_candidate_1m_1y` | ok | `_reports/akela_meta_short/champion_20260512T133459Z/backtests/IDOL/V21_maxxing_bingx_live_candidate_1m_1y/backtest.log` |
| `IDOL/USDT:USDT` | `V21_current_best_tuner_freedommoney_bingx_1m_1y_20260511` | ok | `_reports/akela_meta_short/champion_20260512T133459Z/backtests/IDOL/V21_current_best_tuner_freedommoney_bingx_1m_1y_20260511/backtest.log` |
| `IDOL/USDT:USDT` | `V21_strict_trend_stable_live_static9p38` | ok | `_reports/akela_meta_short/champion_20260512T133459Z/backtests/IDOL/V21_strict_trend_stable_live_static9p38/backtest.log` |
| `FREEDOMMONEY/USDT:USDT` | `V21_freedommoney_bingx_live_candidate_1m_1y` | ok | `_reports/akela_meta_short/champion_20260512T133459Z/backtests/FREEDOMMONEY/V21_freedommoney_bingx_live_candidate_1m_1y/backtest.log` |
| `FREEDOMMONEY/USDT:USDT` | `V21_maxxing_bingx_live_candidate_1m_1y` | ok | `_reports/akela_meta_short/champion_20260512T133459Z/backtests/FREEDOMMONEY/V21_maxxing_bingx_live_candidate_1m_1y/backtest.log` |
| `FREEDOMMONEY/USDT:USDT` | `V21_current_best_tuner_freedommoney_bingx_1m_1y_20260511` | ok | `_reports/akela_meta_short/champion_20260512T133459Z/backtests/FREEDOMMONEY/V21_current_best_tuner_freedommoney_bingx_1m_1y_20260511/backtest.log` |
| `FREEDOMMONEY/USDT:USDT` | `V21_strict_trend_stable_live_static9p38` | ok | `_reports/akela_meta_short/champion_20260512T133459Z/backtests/FREEDOMMONEY/V21_strict_trend_stable_live_static9p38/backtest.log` |
| `MAXXING/USDT:USDT` | `V21_freedommoney_bingx_live_candidate_1m_1y` | ok | `_reports/akela_meta_short/champion_20260512T133459Z/backtests/MAXXING/V21_freedommoney_bingx_live_candidate_1m_1y/backtest.log` |
| `MAXXING/USDT:USDT` | `V21_maxxing_bingx_live_candidate_1m_1y` | ok | `_reports/akela_meta_short/champion_20260512T133459Z/backtests/MAXXING/V21_maxxing_bingx_live_candidate_1m_1y/backtest.log` |
| `MAXXING/USDT:USDT` | `V21_current_best_tuner_freedommoney_bingx_1m_1y_20260511` | ok | `_reports/akela_meta_short/champion_20260512T133459Z/backtests/MAXXING/V21_current_best_tuner_freedommoney_bingx_1m_1y_20260511/backtest.log` |
| `MAXXING/USDT:USDT` | `V21_strict_trend_stable_live_static9p38` | ok | `_reports/akela_meta_short/champion_20260512T133459Z/backtests/MAXXING/V21_strict_trend_stable_live_static9p38/backtest.log` |
| `SUP/USDT:USDT` | `V21_freedommoney_bingx_live_candidate_1m_1y` | ok | `_reports/akela_meta_short/champion_20260512T133459Z/backtests/SUP/V21_freedommoney_bingx_live_candidate_1m_1y/backtest.log` |
| `SUP/USDT:USDT` | `V21_maxxing_bingx_live_candidate_1m_1y` | ok | `_reports/akela_meta_short/champion_20260512T133459Z/backtests/SUP/V21_maxxing_bingx_live_candidate_1m_1y/backtest.log` |
| `SUP/USDT:USDT` | `V21_current_best_tuner_freedommoney_bingx_1m_1y_20260511` | ok | `_reports/akela_meta_short/champion_20260512T133459Z/backtests/SUP/V21_current_best_tuner_freedommoney_bingx_1m_1y_20260511/backtest.log` |
| `SUP/USDT:USDT` | `V21_strict_trend_stable_live_static9p38` | ok | `_reports/akela_meta_short/champion_20260512T133459Z/backtests/SUP/V21_strict_trend_stable_live_static9p38/backtest.log` |

## Night Tuning

| symbol | start cfg | status | tuner summary | log |
| --- | --- | --- | --- | --- |
| `IDOL/USDT:USDT` | `V21_freedommoney_bingx_live_candidate_1m_1y` | ok | `` | `_reports/akela_meta_short/champion_20260512T133459Z/tuner_IDOL.log` |
| `FREEDOMMONEY/USDT:USDT` | `V21_current_best_tuner_freedommoney_bingx_1m_1y_20260511` | ok | `` | `_reports/akela_meta_short/champion_20260512T133459Z/tuner_FREEDOMMONEY.log` |
