# Champion v3.8 vs V21 static9p38

Date: 2026-05-11

## Installed files

- `obw_platform/configs/ENA_champion_v3_8_ssp085_ssmf2.yaml`
- `obw_platform/strategies/cryptomine_pack_dual_compound_v5.py`
- `obw_platform/slippage_orderbook_model_v1.py`
- `docs/champion_v3_8/README_bundle_2026_05_10.md`

The bundle's `data/ena_ohlcv_30s_1y_from_ticks.npz` is already present as
`DB/ena_ohlcv_30s_1y_from_ticks.npz` with the same SHA256. For Python 3.8 runs,
use `DB/ena_ohlcv_30s_1y_from_ticks_compat_np1.npz`.

The bundle's `strategies/cryptomine_pack_dual_compound.py` matches the project
copy exactly. The actual new strategy logic is in
`cryptomine_pack_dual_compound_v5.py`, adding dual-MA timeframe selection for
the short leg via `dualMaEnabled`.

The bundle backtester/core were not installed over the active project versions.
The active core differs only by lazy importing pandas when exporting curves.

## Same-window active backtester comparison

NPZ: `DB/ena_ohlcv_30s_1y_from_ticks_compat_np1.npz`
Window: `2025-03-01T00:00:00+00:00` to `2026-03-01T00:00:00+00:00`

| Config | MTM % | MTM PnL | Realized | Unrealized | MDD MTM % | Ratio | Trades | MC |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `ENA_champion_v3_8_ssp085_ssmf2.yaml` | 3.27 | 6.55 | 73.71 | -67.16 | -21.07 | 0.31 | 4982 | 0 |
| `V21_strict_trend_stable_live_static9p38.yaml` | 18.43 | 36.87 | 65.39 | -28.53 | -15.64 | 2.36 | 6430 | 0 |

Outputs:

- `_reports/ena_champion_v3_8_active_backtester_result.json`
- `_reports/ena_v21_static9p38_active_backtester_result.json`
- `_reports/ena_champion_v3_8_vs_v21_backtest_compare.csv`
- `_reports/ena_v21_live_summary_for_champion_compare.json`

## Interpretation

The new champion has higher realized PnL than V21 on the same one-year window,
but its final open inventory is much more toxic: `-67.16` unrealized versus
V21's `-28.53`. This collapses MTM from realized `+73.71` to MTM `+6.55`.

The new algorithm also fires fewer trades/order events than V21:

- champion: 4982 trades, 8285 order events
- V21: 6430 trades, 12342 order events

This explains why it is not optimizing to the same MTM quality in this checkout:
the improved logic appears to reduce activity and realized churn, but leaves
too much terminal inventory relative to realized profit.

The bundle README mentions an active-backtester reference of MTM `+15.92%`, but
the current project checkout reproduces the bundle/reference result of about
`+3.27%` for this champion.
