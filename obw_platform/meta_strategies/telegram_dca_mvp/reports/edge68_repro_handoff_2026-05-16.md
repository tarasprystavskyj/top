# Edge68 Reproduction Handoff 2026-05-16

Mode: research-only, paper backtests only. Do not touch live/daemon/order execution.

## Confirmed Local Results

The external handoff result around +9% was reproduced locally.

Best-case subset:
- Signals file: `obw_platform/meta_strategies/telegram_dca_mvp/reports/telegram_signals_raw_edge_min5.csv`
- Dataset: filtered `edge_min5`, 83 rows, 68 opened trades, 15 rejected.
- Runner: `obw_platform/meta_strategies/telegram_dca_mvp/run_telegram_simple_baseline_npz.py`
- Parameters: `close_in_zone`, `signal-hard-ttl-sec=3600`, `exit-at-tp=3`, `tp-margin-weights=edge_in_zone`, `move-meta-stop-after-tp=true`.

Results on old event-window NPZ:
- NPZ: `DB/telegram_signals_1m_event_windows_bingx.npz`
- PnL: `+9.2276%`
- MDD: `-1.1122%`
- PnL/MDD: `8.2967`
- Trades: `68`

Results on 720h event-window NPZ:
- NPZ: `DB/telegram_signals_1m_event_windows_720h_bingx.npz`
- PnL: `+9.2276%`
- MDD: `-1.1122%`
- PnL/MDD: `8.2967`
- Trades: `68`

The identical result means these 68 trades resolve before the old shorter window ends.

## Full-Universe Control

Same execution profile on all extracted signals remains negative.

- Signals file: `DB/telegram_signal_standard_bt/telegram_signals_extracted.csv`
- NPZ: `DB/telegram_signals_1m_event_windows_720h_bingx.npz`
- PnL: `-16.1257%`
- MDD: `-20.1267%`
- PnL/MDD: `-0.8012`
- Opened trades: `251`
- Missing market data: `7`
- Rejected: `54`

## Loop Implication

The +9.23% result is a valid local reproduction, but it is not a full-universe strategy. Treat it as an evidence subset. The next research loop should search for a non-leaky selector or execution state that recovers this subset behavior without using future outcome information.

Priority hypotheses:
- Reconstruct exactly how `edge_min5` was selected and classify which inputs are known at signal time.
- Compare selected vs rejected cohorts by signal-time-only features: entry width, TP/SL RR, side, symbol, hour/day, signal text structure, entry fill latency, first-hour behavior.
- Walk-forward any selector. A selector that only works in-sample is not promotable.
- Keep no-DCA first. DCA variants may be compared only after a defensible pure-execution selector exists.
