# HYPE VeronicaUA Initial Equity 100 / No Warmup / No Trend Check

Updated: 2026-05-24T04:38Z

Scope:
- Lead: `4300516091842181632`
- Source: Binance public copy closed position history
- Positions: `122`
- Test window: `2026-01-09T19:16:26Z` .. `2026-05-24T01:01:12Z`
- Entry side: direct lead side, all `HYPEUSDT LONG`
- Initial equity: `100`
- Target notional: `100`
- Trend gating: none
- Warmup/trend determination for entry: none

Notes:
- The historical signal-side simulator already did not use trend to flip, block, suppress, resize, or exit entries.
- Prior loop warmup affected the OHLCV collection window only. The closed-position backtest uses each Binance position's own open/close timestamps.
- Future `run_binance_430051_hype_v21_loop.py` waves now pass `--initial-equity 100` into `compare_binance_copy_positions_dca.py`.

Top Results

| rank | variant | label | net % | /30d % | PF | max DD % | win % | avg DCA fills |
|---:|---|---|---:|---:|---:|---:|---:|---:|
| 1 | `long_low_exposure` | `dca3` | 157.4090 | 35.1780 | 36.6722 | -2.0189 | 95.08 | 2.03 |
| 2 | `baseline` | `dca3` | 156.6669 | 35.0121 | 31.3719 | -2.1271 | 95.08 | 2.18 |
| 3 | `long_aggressive_second_leg` | `dca3` | 155.7851 | 34.8150 | 29.4678 | -2.1670 | 95.08 | 2.20 |
| 4 | `long_high_tp` | `dca3` | 155.7000 | 34.7960 | 32.6319 | -2.0960 | 95.08 | 2.11 |
| 5 | `long_conservative_wide_grid` | `dca3` | 154.7445 | 34.5824 | 52.2102 | -1.7334 | 97.54 | 1.75 |
| 6 | `long_balanced_hype_grid` | `dca3` | 154.6316 | 34.5571 | 36.9013 | -2.0190 | 95.08 | 2.01 |

Best current choice by net return remains `long_low_exposure / dca3`.
The lowest maxDD among top candidates is `long_conservative_wide_grid / dca3`, but it gives slightly lower net return.

