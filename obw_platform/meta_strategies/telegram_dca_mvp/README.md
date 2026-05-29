# Telegram DCA MVP

Static runner for `Telegram open signal -> V21 DCA cycle -> Telegram TP/SL meta-exit`.

Primary local inputs:

- `telegram_standard_bt_bundle/telegram_signal_standard_bt/telegram_signals_extracted.csv`
- `DB/telegram_signals_3m_7200b_bingx.npz`
- `obw_platform/configs/V21_strict_trend_stable_live_static9p38.yaml`

The local CSV has 312 open signals. No static `channel_exit` event stream is
present in this worktree, so manual Telegram closes are not included yet.
