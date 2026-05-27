# Telegram Signal DCA Meta Strategy

Research lane for comparing plain Telegram signal execution against V21-style
DCA execution on the same signal rows and the same OHLCV DB.

This is paper/backtest only. It does not place orders, read `.env`, or call an
exchange. Price data must already exist in a SQLite `price_indicators` table.

## What It Compares

- `plain`: open from the Telegram signal entry zone, then use Telegram TP1/TP2
  partials, TP3 final exit, and Telegram SL.
- `v21_dcaN`: same entry and same Telegram exits, but add up to `N` V21 DCA
  fills using `V21_strict_trend_stable_live_static9p38.yaml` sizing and ladder
  steps.

The baseline is `dca_count=0`, so the comparison isolates the DCA layer while
keeping signal timing, side, TP, SL, fees, and slippage constant.

## Example

```powershell
python obw_platform\meta_strategies\telegram_signal_dca\telegram_signal_dca_compare.py `
  --signals-csv telegram_standard_bt_bundle\runs\kanalbacktest1_20260519_130722\nevskiyh_deep_valid_replay_signals.csv `
  --price-db telegram_standard_bt_bundle\runs\kanalbacktest1_20260519_130722\nevskiyh_deep_signal_windows_3m_72h_bingx.db `
  --v21-config obw_platform\configs\V21_strict_trend_stable_live_static9p38.yaml `
  --out-dir obw_platform\meta_strategies\telegram_signal_dca\reports\nevskiyh_v21 `
  --dca-counts 0,1,2,3,4,5
```

Outputs:

- `dca_summary.csv` - one row per variant.
- `<variant>_trades.csv` - exits and realized PnL.
- `<variant>_equity.csv` - MTM curve.
- `<variant>_symbol_summary.csv` - per-symbol PnL and DCA usage.
- `manifest.json` - run inputs and key metadata.

## Safety

Keep this lane separate from live Telegram daemon work. The script accepts only
local CSV/SQLite/YAML inputs and never sends orders.
