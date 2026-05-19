# Telegram Signal DCA Comparison - 2026-05-19

Mode: research/backtest only.

## Future-Leak Fix

Do not select "profitable symbols" from the same period being evaluated.

For darkknight, the non-leaky test used:

- train: first 70% of chronological signals, 218 rows
- OOS: last 30% of chronological signals, 94 rows
- selector source: train-only plain execution PnL by base symbol
- OOS execution: only later signals whose base was selected from train

Strict selector:

- rule: train symbol PnL > 0 and at least 5 closed train trade rows
- selected bases: AAVE, ADA, DOT, FET, NEAR
- OOS selected signals: 11

Lenient selector:

- rule: train symbol PnL > 0 with any closed train trade count
- selected bases: AAVE, ADA, BNB, CRV, DOT, FET, IMX, JUP, LINK, NEAR, NEO, OP, ORDI, TAO, TIA
- OOS selected signals: 37

## Darkknight Causal OOS

Execution settings:

- NPZ: `obw_platform/meta_strategies/telegram_dca_mvp/worker_bundle/npz_720h_parts/telegram_signals_1m_event_windows_720h_bingx.npz`
- entry: `close_in_zone`
- TTL: 72h, hard lag 3600s
- exit: TP2, `edge_in_zone`, move meta-stop after TP
- DCA: `V21_strict_trend_stable_live_static9p38.yaml`

| Selector | Variant | Signals | Opened | PnL | MDD | PnL/MDD |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| strict min5 | plain | 11 | 8 | +0.4093% | -0.1140% | 3.5918 |
| strict min5 | DCA1 1.5x | 11 | 8 | +0.4093% | -0.8983% | 0.4557 |
| strict min5 | DCA2 2.0x | 11 | 8 | +1.0314% | -0.9991% | 1.0324 |
| strict min5 | DCA3 2.5x | 11 | 8 | +1.7939% | -0.9939% | 1.8050 |
| lenient anyN | plain | 37 | 26 | +0.0148% | -1.3145% | 0.0112 |
| lenient anyN | DCA1 1.5x | 37 | 26 | +0.6691% | -2.2359% | 0.2993 |
| lenient anyN | DCA2 2.0x | 37 | 26 | +1.1985% | -2.8390% | 0.4221 |
| lenient anyN | DCA3 2.5x | 37 | 26 | +2.5331% | -3.4634% | 0.7314 |

Interpretation: the non-leaky darkknight filter is positive OOS, but the strict sample is very small. DCA increased PnL in this OOS slice but also increased drawdown; this is not yet enough for promotion.

## Multi-Channel V21 Wrapper

Input:

- signals: `C:/python_scripts/top_1_telegram_signals/telegram_standard_bt_bundle/runs/kanalbacktest1_20260519_130722/all_channels_replay_signals.csv`
- DB: `C:/python_scripts/top_1_telegram_signals/telegram_standard_bt_bundle/runs/kanalbacktest1_20260519_130722/telethon_signal_windows_3m_72h_bingx.db`
- config: `obw_platform/configs/V21_strict_trend_stable_live_static9p38.yaml`

| Channel | Signals | Opened | Best Variant | Best PnL | Plain PnL | Note |
| --- | ---: | ---: | --- | ---: | ---: | --- |
| Nevskiyh | 30 | 2 | DCA5 | +0.4104% | +0.0352% | Very small opened sample |
| Treyding_Signaly_Kripto | 13 | 6 | DCA5 | +0.1097% | +0.0101% | Very small sample |
| topslivs | 75 | 26 | DCA5 | +0.1829% | +0.0797% | DCA improved PnL, worse MDD |
| White_Ghosto | 15 | 12 | plain | -0.0495% | -0.0495% | DCA made it worse |
| kriptaw | 15 | 12 | plain | -0.0495% | -0.0495% | Same signal set as White_Ghosto in this run |

Raw output directories:

- `obw_platform/meta_strategies/telegram_signal_dca/reports/all_channels_v21_20260519/`
- `obw_platform/meta_strategies/telegram_dca_mvp/reports/causal_profitable_symbols_20260519/`

