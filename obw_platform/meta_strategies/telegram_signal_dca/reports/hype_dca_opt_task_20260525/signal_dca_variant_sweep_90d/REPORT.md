# HYPE ie500 Signal+DCA Variant Sweep 90d

Research-only local test. No live orders, no secrets, no network, no private scraping.

## Inputs

- OHLC NPZ: `C:\python_scripts\top_1_dev_veronica\obw_platform\meta_strategies\telegram_signal_dca\reports\binance_430051_hype_v21_loop_20260523\binance_4300516091842181632_hype_universe_1m_20250524_20260524.npz`
- Signal CSV: `C:\python_scripts\top_1\obw_platform\meta_strategies\telegram_signal_dca\reports\hype_dca_opt_task_20260525\signal_chart_artifact\signal_events.csv`
- Window: 2026-02-23T02:58:00Z to 2026-05-24T02:58:00Z
- Signals: HYPEUSDT LONG OPEN/CLOSE timestamps only; Binance avgCost/avgClosePrice ignored except as CSV metadata.
- Parity note: variants are Python-emulator approximations, not exact TradingView broker-emulator parity.

## Baseline

- Net: 113.088815% | Max DD: -19.889437% | Orders: 755

## Best Results

- Best net overall: `signal_aware_tp` {"fresh_callback_percent": 0.25, "fresh_tp_percent": 1.2, "freshness_ms": 86400000} -> net 119.908969%, max DD -19.712136%
- Best beating baseline without worse drawdown: `signal_aware_tp` {"fresh_callback_percent": 0.25, "fresh_tp_percent": 1.2, "freshness_ms": 86400000} -> net 119.908969%, max DD -19.712136%

## Top Rows By Net

| rank | variant | params | net pct | max DD pct | orders | first buys | DCA buys | full TP closes | open cost |
|---:|---|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | signal_aware_tp | `{"fresh_callback_percent": 0.25, "fresh_tp_percent": 1.2, "freshness_ms": 86400000}` | 119.908969 | -19.712136 | 529 | 141 | 248 | 140 | 1114.063626 |
| 2 | signal_aware_tp | `{"fresh_callback_percent": 0.15, "fresh_tp_percent": 1.2, "freshness_ms": 86400000}` | 118.134683 | -19.712136 | 535 | 143 | 250 | 142 | 1102.768743 |
| 3 | signal_aware_tp | `{"fresh_callback_percent": 0.25, "fresh_tp_percent": 1.0, "freshness_ms": 21600000}` | 117.720813 | -19.712136 | 667 | 181 | 306 | 180 | 1099.194016 |
| 4 | signal_aware_tp | `{"fresh_callback_percent": 0.25, "fresh_tp_percent": 1.0, "freshness_ms": 86400000}` | 115.543833 | -19.712136 | 555 | 148 | 260 | 147 | 1091.949757 |
| 5 | signal_aware_tp | `{"fresh_callback_percent": 0.15, "fresh_tp_percent": 1.2, "freshness_ms": 7200000}` | 115.447925 | -19.889437 | 714 | 197 | 321 | 196 | 1087.719022 |
| 6 | signal_aware_tp | `{"fresh_callback_percent": 0.25, "fresh_tp_percent": 1.2, "freshness_ms": 7200000}` | 115.057798 | -19.889437 | 708 | 195 | 319 | 194 | 1085.749409 |
| 7 | signal_aware_tp | `{"fresh_callback_percent": 0.25, "fresh_tp_percent": 1.2, "freshness_ms": 21600000}` | 114.716036 | -19.712136 | 653 | 178 | 298 | 177 | 1084.023978 |
| 8 | signal_aware_tp | `{"fresh_callback_percent": 0.15, "fresh_tp_percent": 1.0, "freshness_ms": 86400000}` | 114.456988 | -20.216647 | 555 | 148 | 260 | 147 | 1084.176345 |
| 9 | signal_aware_tp | `{"fresh_callback_percent": 0.15, "fresh_tp_percent": 1.0, "freshness_ms": 21600000}` | 114.213445 | -20.216647 | 669 | 183 | 304 | 182 | 1081.486576 |
| 10 | signal_aware_tp | `{"fresh_callback_percent": 0.15, "fresh_tp_percent": 0.8, "freshness_ms": 86400000}` | 114.070014 | -20.216647 | 612 | 166 | 281 | 165 | 1086.951021 |
| 11 | signal_aware_tp | `{"fresh_callback_percent": 0.15, "fresh_tp_percent": 1.2, "freshness_ms": 21600000}` | 113.757466 | -19.712136 | 665 | 181 | 304 | 180 | 1079.184503 |
| 12 | baseline | `{}` | 113.088815 | -19.889437 | 755 | 211 | 334 | 210 | 1081.968936 |

## Variant Limitations

- `entry_gate`: gates only new first/restart cycles; existing cycles still manage TP/DCA normally.
- `sizing_boost`: sets the per-cycle base quantity from signal recency at cycle start; later DCA uses the cycle base, matching the emulator's existing cycle-base behavior.
- `score_blend`: uses an exponential decay from the latest signal OPEN, with active signals forced to score 1.0.
- `dca_permission`: signal-only rows do not implement a full MA/trend model; the proxy row is a permissive local price check, not TradingView MA parity.
- `signal_aware_tp`: changes TP/trailing only on bars where the signal is active/recent; this is an approximation because TradingView intrabar state is not reproduced.
