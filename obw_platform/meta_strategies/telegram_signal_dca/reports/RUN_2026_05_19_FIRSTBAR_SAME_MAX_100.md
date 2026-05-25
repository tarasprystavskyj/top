# First-Bar Same-Max Telegram DCA Comparison - 2026-05-19

Mode: research/backtest only.

## Why This Run Exists

The earlier comparison was not clean enough:

- Some DCA variants could use more total notional than plain.
- `close_in_zone` entry answered "did price later enter the Telegram entry zone?", not "was the signal direction good from message time?".

This run changes both:

- capital mode: same planned max notional = 100 USDT
- plain: initial notional 100
- DCA1 1.5x: initial 66.67, max planned total 100
- DCA2 2.0x: initial 50, max planned total 100
- DCA3 2.5x: initial 40, max planned total 100
- entry mode: `first_bar`, enter on the first available candle after signal time

## Multi-Channel First-Bar Same-Max 100

Input:

- signals: `C:/python_scripts/top_1_telegram_signals/telegram_standard_bt_bundle/runs/kanalbacktest1_20260519_130722/all_channels_replay_signals.csv`
- DB: `C:/python_scripts/top_1_telegram_signals/telegram_standard_bt_bundle/runs/kanalbacktest1_20260519_130722/telethon_signal_windows_3m_72h_bingx.db`
- DCA config: `obw_platform/configs/V21_strict_trend_stable_live_static9p38.yaml`

Monthly extrapolation is simple linear normalization: `equity_return_pct * 30 / calendar_days`.
It is not a forecast.

| Channel | Window | Days | Signals | Opened | Plain Return | Plain / 30d | Best Variant | Best Return | Best / 30d |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: |
| Nevskiyh | 2026-04-10..2026-04-30 | 20.0 | 30 | 29 | +2.56% | +3.85% | DCA3 | +3.53% | +5.31% |
| topslivs | 2026-01-22..2026-05-18 | 115.3 | 75 | 24 | +0.17% | +0.04% | DCA2 | +1.64% | +0.43% |
| Treyding_Signaly_Kripto | 2026-05-15..2026-05-19 | 3.4 | 13 | 9 | +0.04% | +0.39% | DCA5 | +0.11% | +1.01% |
| White_Ghosto | 2026-04-16..2026-04-29 | 13.0 | 15 | 15 | -1.91% | -4.42% | DCA5 | -1.50% | -3.47% |
| kriptaw | 2026-04-16..2026-04-29 | 13.0 | 15 | 15 | -1.91% | -4.42% | DCA5 | -1.50% | -3.47% |

Notes:

- Nevskiyh and topslivs are the only useful candidates in this small sample.
- White_Ghosto and kriptaw are identical in this dataset and negative even after DCA.
- `opened` can still be lower than `signals` because the DB may not contain that symbol/time, or concurrent same-symbol positions can block another signal in this wrapper.

## Darkknight Causal OOS First-Bar Same-Max 100

Selector remains non-leaky:

- train: first 70% of darkknight signals
- OOS: last 30%
- profitable symbols selected only from train plain execution

| Selector | Window | Days | Variant | Signals | Opened | Return | Return / 30d | MDD | PnL/MDD |
| --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| strict min5 | 2025-12-05..2026-05-06 | 152.1 | plain | 11 | 11 | +0.92% | +0.18% | -0.1422% | 6.4987 |
| strict min5 | 2025-12-05..2026-05-06 | 152.1 | DCA1 1.5x | 11 | 11 | +0.77% | +0.15% | -1.2684% | 0.6039 |
| strict min5 | 2025-12-05..2026-05-06 | 152.1 | DCA2 2.0x | 11 | 11 | +1.85% | +0.37% | -1.3812% | 1.3399 |
| strict min5 | 2025-12-05..2026-05-06 | 152.1 | DCA3 2.5x | 11 | 11 | +2.56% | +0.51% | -1.3449% | 1.9063 |
| lenient anyN | 2025-11-28..2026-05-06 | 159.1 | plain | 37 | 35 | +0.73% | +0.14% | -1.2006% | 0.6051 |
| lenient anyN | 2025-11-28..2026-05-06 | 159.1 | DCA1 1.5x | 37 | 35 | +2.47% | +0.47% | -2.2748% | 1.0851 |
| lenient anyN | 2025-11-28..2026-05-06 | 159.1 | DCA2 2.0x | 37 | 35 | +1.77% | +0.33% | -3.0151% | 0.5864 |
| lenient anyN | 2025-11-28..2026-05-06 | 159.1 | DCA3 2.5x | 37 | 35 | +3.38% | +0.64% | -2.9695% | 1.1394 |

Interpretation:

- First-bar tests answer signal direction quality better than entry-zone tests.
- Apples-to-apples same-max capital removes the "DCA used more money" bias.
- DCA still improves PnL for darkknight lenient OOS and for Nevskiyh/topslivs, but it often increases drawdown.
- Sample sizes remain too small for live promotion.
