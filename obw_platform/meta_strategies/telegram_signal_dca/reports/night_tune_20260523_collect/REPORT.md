# Night Rough Tune

Paper/backtest only. No live orders. No secrets are read or printed.

- Latest scan: `2026-05-23T10:21:05.093055Z`
- TTL grid: `24, 48, 72, 96`
- DCA grid: `0,1,2,3`
- Slippage grid requested: `9.38, 18.7` bp

## Telegram Sources

| source | status | signals | price DB | notes |
|---|---|---|---|---|
| darkknighttrade | ran 4 jobs | `/var/www/vps2.happyuser.info/top/top_1/obw_platform/meta_strategies/telegram_signal_dca/reports/night_tune_20260523_collect/telegram/darkknighttrade/darkknighttrade_signals.csv` | `/var/www/vps2.happyuser.info/top/top_1/obw_platform/meta_strategies/telegram_signal_dca/reports/night_tune_20260523_collect/telegram/darkknighttrade/darkknighttrade_price_indicators_3m_7200b.sqlite` | stress slippage 18.7bp requires config-level support; recorded as pending |
| Nevskiyh | blocked | `/var/www/vps2.happyuser.info/top/top_1/obw_platform/meta_strategies/telegram_signal_dca/reports/night_tune_20260523_collect/telegram/Nevskiyh/Nevskiyh_signals.csv` | `` | missing source-specific SQLite price_indicators DB |
| topslivs | blocked | `/var/www/vps2.happyuser.info/top/top_1/obw_platform/meta_strategies/telegram_signal_dca/reports/night_tune_20260523_collect/telegram/topslivs/topslivs_signals.csv` | `` | missing source-specific SQLite price_indicators DB |
| Treyding_Signaly_Kripto | blocked | `/var/www/vps2.happyuser.info/top/top_1/obw_platform/meta_strategies/telegram_signal_dca/reports/night_tune_20260523_collect/telegram/Treyding_Signaly_Kripto/Treyding_Signaly_Kripto_signals.csv` | `` | missing source-specific SQLite price_indicators DB |

## Binance Copy Leads

| lead | status | positions CSV | notes |
|---|---|---|---|
| 4728671486012660992 | ran 1 jobs | `/var/www/vps2.happyuser.info/top/top_1/obw_platform/meta_strategies/telegram_signal_dca/reports/night_tune_20260523_collect/binance_copy/4728671486012660992/position_history_normalized.csv` | stress slippage 18.7bp requires config-level support; recorded as pending |
| 4751838302089254401 | ran 1 jobs | `/var/www/vps2.happyuser.info/top/top_1/obw_platform/meta_strategies/telegram_signal_dca/reports/night_tune_20260523_collect/binance_copy/4751838302089254401/position_history_normalized.csv` | stress slippage 18.7bp requires config-level support; recorded as pending |
| 4906010685108267264 | ran 1 jobs | `/var/www/vps2.happyuser.info/top/top_1/obw_platform/meta_strategies/telegram_signal_dca/reports/night_tune_20260523_collect/binance_copy/4906010685108267264/position_history_normalized.csv` | stress slippage 18.7bp requires config-level support; recorded as pending |

## Ranking

Ranking by max-cap normalized return, PF, maxDD, and trade count will be generated after source-specific input data is present.
