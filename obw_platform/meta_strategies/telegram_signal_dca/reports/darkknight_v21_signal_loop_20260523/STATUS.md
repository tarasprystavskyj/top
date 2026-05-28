# DarkKnight V21 Signal-Side Loop

Backtest/tune only. No live orders. No secrets are printed.

- Updated: `2026-05-24T03:56:47.929774Z`
- Status: `cycle_complete`
- tmux session: `darkknight_v21_signal_loop`
- Philosophy: consilium handles warmup/trend determination; execution is signal-side single-leg only; no trend reaction.
- Channel: `darkknighttrade`
- Source CSV: `obw_platform/meta_strategies/telegram_signal_dca/reports/darkknight_v21_signal_loop_20260523/data/darkknighttrade_signals.csv`
- Universe file: `obw_platform/meta_strategies/telegram_signal_dca/reports/darkknight_v21_signal_loop_20260523/data/darkknighttrade_universe_signal_history.txt`
- NPZ: `obw_platform/meta_strategies/telegram_signal_dca/reports/darkknight_v21_signal_loop_20260523/data/darkknighttrade_3m_156550b_signal_window.npz`
- Price DB: `obw_platform/meta_strategies/telegram_signal_dca/reports/darkknight_v21_signal_loop_20260523/data/darkknighttrade_price_indicators_3m_156550b_signal_window.sqlite`
- Report dir: `obw_platform/meta_strategies/telegram_signal_dca/reports/darkknight_v21_signal_loop_20260523`

## Window

- Signal start: `2025-07-23T10:37:43Z`
- Signal end: `2026-05-22T12:06:26Z`
- OHLCV warmup days: `14`
- Timeframe: `3m`
- Requested bars: `156550`
- TTL grid hours: `24.0, 48.0, 72.0, 96.0, 120.0, 168.0`
- Entry mode grid: `first_bar, touch_zone, close_in_zone`

## Universe

- Symbols: `41`

```
ROSE GRT INJ PYTH RENDER ICP LINK ONDO SUI FET DOT JTO IMX AAVE ADA NEAR ORDI LDO BCH ETC XRP ATOM APT OP SOL ENS PENDLE VET JUP NEO EGLD JASMY HBAR DOGE SEI XLM GALA ETHFI WLD TAO MORPHO
```

## Last Step

```json
{
  "cmd": [
    "/var/www/vps2.happyuser.info/top/backtest_SK/.venv38/bin/python",
    "/var/www/vps2.happyuser.info/top/top_1/obw_platform/meta_strategies/telegram_signal_dca/compare_channels_v21.py",
    "--signals-csv",
    "obw_platform/meta_strategies/telegram_signal_dca/reports/darkknight_v21_signal_loop_20260523/data/darkknighttrade_signals.csv",
    "--price-db",
    "obw_platform/meta_strategies/telegram_signal_dca/reports/darkknight_v21_signal_loop_20260523/data/darkknighttrade_price_indicators_3m_156550b_signal_window.sqlite",
    "--v21-config",
    "/var/www/vps2.happyuser.info/top/top_1/obw_platform/configs/V21_strict_trend_stable_live_static9p38.yaml",
    "--out-dir",
    "obw_platform/meta_strategies/telegram_signal_dca/reports/darkknight_v21_signal_loop_20260523/grid/ttl_168.0h/close_in_zone",
    "--dca-counts",
    "0",
    "--ttl-hours",
    "168.0",
    "--entry-mode",
    "close_in_zone",
    "--side",
    "both",
    "--capital-mode",
    "same_max",
    "--target-notional",
    "100.0"
  ],
  "started_at": "2026-05-24T03:56:09.145611Z",
  "finished_at": "2026-05-24T03:56:47.926576Z",
  "returncode": 0,
  "timed_out": false,
  "log": "obw_platform/meta_strategies/telegram_signal_dca/reports/darkknight_v21_signal_loop_20260523/grid/ttl_168.0h/close_in_zone/compare.log"
}
```
