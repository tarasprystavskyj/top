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

| Channel | Signals | Opened | Plain PnL | Plain MDD | Best Variant | Best PnL | Best MDD |
| --- | ---: | ---: | ---: | ---: | --- | ---: | ---: |
| Nevskiyh | 30 | 29 | +25.6031 | -4.0895% | DCA3 | +35.2823 | -3.0933% |
| topslivs | 75 | 24 | +1.6508 | -2.6329% | DCA2 | +16.4485 | -2.5856% |
| Treyding_Signaly_Kripto | 13 | 9 | +0.4409 | -0.8596% | DCA5 | +1.1300 | -0.7117% |
| White_Ghosto | 15 | 15 | -19.0865 | -2.8835% | DCA5 | -14.9963 | -2.2857% |
| kriptaw | 15 | 15 | -19.0865 | -2.8835% | DCA5 | -14.9963 | -2.2857% |

Notes:

- Nevskiyh and topslivs are the only useful candidates in this small sample.
- White_Ghosto and kriptaw are identical in this dataset and negative even after DCA.
- `opened` can still be lower than `signals` because the DB may not contain that symbol/time, or concurrent same-symbol positions can block another signal in this wrapper.

## Darkknight Causal OOS First-Bar Same-Max 100

Selector remains non-leaky:

- train: first 70% of darkknight signals
- OOS: last 30%
- profitable symbols selected only from train plain execution

| Selector | Variant | Signals | Opened | PnL | MDD | PnL/MDD |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| strict min5 | plain | 11 | 11 | +0.9239% | -0.1422% | 6.4987 |
| strict min5 | DCA1 1.5x | 11 | 11 | +0.7660% | -1.2684% | 0.6039 |
| strict min5 | DCA2 2.0x | 11 | 11 | +1.8508% | -1.3812% | 1.3399 |
| strict min5 | DCA3 2.5x | 11 | 11 | +2.5638% | -1.3449% | 1.9063 |
| lenient anyN | plain | 37 | 35 | +0.7265% | -1.2006% | 0.6051 |
| lenient anyN | DCA1 1.5x | 37 | 35 | +2.4685% | -2.2748% | 1.0851 |
| lenient anyN | DCA2 2.0x | 37 | 35 | +1.7679% | -3.0151% | 0.5864 |
| lenient anyN | DCA3 2.5x | 37 | 35 | +3.3834% | -2.9695% | 1.1394 |

Interpretation:

- First-bar tests answer signal direction quality better than entry-zone tests.
- Apples-to-apples same-max capital removes the "DCA used more money" bias.
- DCA still improves PnL for darkknight lenient OOS and for Nevskiyh/topslivs, but it often increases drawdown.
- Sample sizes remain too small for live promotion.

