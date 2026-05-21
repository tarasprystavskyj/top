# Telegram Signal DCA Meta Strategy

Research lane for comparing plain Telegram signal execution against V21 signal
wrappers on the same signal rows and the same OHLCV DB.

For paper/live Telegram execution, the intended meta-strategy is not the old
simplified DCA overlay. Telegram signals are external gates into the real V21
sub-strategy from `obw_platform/configs/V21_strict_trend_stable_live_static9p38.yaml`:

- LONG signal: instantiate only
  `strategies.cryptomine_pack_dual_full.CryptomineLongPackAdaptiveEven`.
- SHORT signal: instantiate only
  `strategies.cryptomine_pack_dual_full.CryptomineShortPackAdaptiveEven`.
- The opposite/hedge leg is not enabled for that signal.
- Delegated source capital overrides `equityForSizingUSDT`; the active side
  gets `baseOrderPctEq=5.0`.

The paper/live listener uses this by default via:

```powershell
python obw_platform\telegram_signal_tools\telegram_signal_paper_live_daemon.py `
  --strategy-mode v21 `
  --notional 100 `
  --entry-policy touch `
  --monitor-exits
```

Use `--strategy-mode legacy_dca` only to reproduce the older simplified overlay.

This is paper/backtest only. It does not place orders, read `.env`, or call an
exchange. Price data must already exist in a SQLite `price_indicators` table.

## What It Compares

- `plain`: open from the Telegram signal entry zone, then use Telegram TP1/TP2
  partials, TP3 final exit, and Telegram SL.
- Historical `v21_dcaN` reports are a simplified DCA overlay that used
  `V21_strict_trend_stable_live_static9p38.yaml` sizing and ladder steps.
- Production paper/live work must use
  `meta_strategies.v21_external_signal_wrapper.V21ExternalSignalLong` and
  `V21ExternalSignalShort`. Those classes wrap the real
  `strategies.cryptomine_pack_dual_full.CryptomineLongPackAdaptiveEven` /
  `CryptomineShortPackAdaptiveEven` classes and only gate new entries by the
  Telegram signal. Once a one-leg signal opens, DCA, TP, sub-sells, stale exits,
  and sync behavior are delegated to the real V21 sub-strategy.

The baseline is `dca_count=0`, so the comparison isolates the DCA layer while
keeping signal timing, side, TP, SL, fees, and slippage constant.

## V21 External-Signal Wrapper

Use the wrapper when a Telegram signal should enable exactly one V21 leg:

- LONG Telegram signal -> long V21 leg only.
- SHORT Telegram signal -> short V21 leg only.
- No opposite/hedged leg for the same external signal.
- Override delegated capital with `external_signal_v21.delegated_capital_usdt`.
- Override initial order sizing with `external_signal_v21.base_order_pct_eq`
  (`5.0` raises the base sizing input; the real V21 trend/regime/vol adaptive
  sizing path can still adjust the actual first order, subject to `minOrderUSDT`).

Minimal config fragment:

```yaml
strategy_class_long: meta_strategies.v21_external_signal_wrapper.V21ExternalSignalLong
strategy_class_short: meta_strategies.v21_external_signal_wrapper.V21ExternalSignalShort
external_signal_v21:
  delegated_capital_usdt: 100.0
  base_order_pct_eq: 5.0
  delegate_strategy_class_long: strategies.cryptomine_pack_dual_full.CryptomineLongPackAdaptiveEven
  delegate_strategy_class_short: strategies.cryptomine_pack_dual_full.CryptomineShortPackAdaptiveEven
  signals_file: obw_platform/meta_strategies/telegram_signal_dca/reports/live_signals.json
```

Generate a full wrapped config from the real V21 YAML:

```powershell
python obw_platform\meta_strategies\telegram_signal_dca\build_v21_external_signal_config.py `
  --delegated-capital-usdt 100 `
  --base-order-pct-eq 5
```

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

## Smoke Test

```powershell
python -m unittest obw_platform.meta_strategies.telegram_signal_dca.test_telegram_v21_one_leg_wrapper_smoke
```

This checks that LONG/SHORT Telegram signals instantiate exactly one V21 class
and that delegated capital plus `baseOrderPctEq=5.0` drive the first order.

## Safety

Keep this lane separate from live Telegram daemon work. The script accepts only
local CSV/SQLite/YAML inputs and never sends orders.
