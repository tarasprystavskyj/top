# Telegram DCA / all-signals research bundle for next agent

Generated UTC: 2026-05-16T11:40:19.730568+00:00

## Purpose

This bundle is for continuing the Telegram-signal execution research without redoing the whole conversation.
It contains the patched runners, full all-signals inputs, MFE/TP1 stats, strategy-version summaries, and the latest research conclusion.

**Do not treat the current results as live-ready.** This is backtest/research only.

## Critical context

- Full universe has 312 Telegram entry signals; 305 have market data.
- Close messages without a coin/symbol must close only the latest still-open position, not all positions.
- Close messages with explicit symbol close only that symbol.
- Only explicit “all positions closed” text may be global close-all.
- Current NPZ is an event-window cache, not guaranteed continuous market history.
- Long no-SL / BE recovery tests are therefore only diagnostic unless rerun on continuous OHLCV.

## Main current conclusions

1. Directional edge exists: MFE vs TP1 is strong, especially over 24h/72h.
2. Full Telegram lifecycle to TP2/TP3 is structurally negative on all signals.
3. The curve is not destroyed by one bad day; it is monotonic because small wins are repeatedly erased by large losses.
4. Removing hard SL and using 1x BE/recovery is promising, but changes risk from realized loss to frozen capital + MTM drawdown.
5. TP-grid scaling by symbol+side alone did not solve the problem; it must be secondary to risk/freeze/liquidation filters.
6. Best diagnostic direction so far: 1x, no normal hard SL, DCA recovery cap 1.5x–2x, target around 0.5 * TP1, max-hold 7–14d, with continuous data required.
7. 2026-05-16 update: a new handoff result was reproduced locally. `raw_edge_min5` + TP3 + `edge_in_zone` + move stop after TP gives 68 opened trades, PnL `+9.2276%`, MDD `-1.1122%`, PnL/MDD `8.2967` on the 720h NPZ.
8. The same TP3 execution on all 312 signals remains negative: 251 opened trades, PnL `-16.1257%`, MDD `-20.1267%`.
9. `raw_edge_min5` is best treated as a diagnostic in-sample symbol-level selector until proven otherwise. It consists of the positive raw-edge symbols `DOT, ORDI, JUP, NEAR, AAVE, ADA, ENS, APT, OP`.

## Important measured results

See `reports/strategy_versions_selected_comparison.csv`.

Key rows:

| Class | Strategy | PnL | MDD | Note |
|---|---:|---:|---:|---|
| official baseline | fixed thirds TP3 + close events | -26.70% | -27.64% | bad full lifecycle |
| official baseline | TP2 50/50 + close events | -26.22% | -27.21% | bad full lifecycle |
| structural impulse | 25% TP1, zone stop, 24h | -7.93% | -8.85% | much less bad |
| non-fit filter | RR TP1/SL >= 1, 25% TP1 target | +0.06% | -2.16% | tiny sample, near BE |
| no-hard-SL recovery | 1 add / 1.5x / 14d / 0.5 TP1 | +1.93% | -13.03% | first positive, weak PnL/MDD |
| no-hard-SL recovery | 2 adds / 2x / 14d / 0.5 TP1 | +3.90% | -16.03% | better PnL, bigger DD |

## Bundle layout

```text
local_test_bundle/
  obw_platform/
    meta_strategies/telegram_dca_mvp/
      run_telegram_simple_baseline_npz.py     # perf-patched official runner
      run_telegram_dca_mvp_npz.py             # perf-patched official runner
      reports/telegram_signals_raw_edge_min5.csv
      reports/telegram_signals_raw_and_dca_edge_min5.csv
    strategies/cryptomine_pack_dual_full.py
    configs/V21_strict_trend_stable_live_static9p38.yaml
  DB/telegram_signal_standard_bt/
    telegram_signals_extracted.csv
    telegram_channel_exit_events.csv
research/
  research_strategy_versions_relative.py      # main experimental harness, relative paths
  research_strategy_versions.py               # original hardcoded sandbox version
  all_signals_fast_sim.py
  fast_tg_sim.py
reports/
  strategy_versions_selected_comparison.csv
  strategy_versions_gap_aware_top20.csv
  strategy_versions_research/*.csv/json
  all_signals_mfe_vs_tp1_summary.json
  all_signals_mfe_vs_tp1_rows.csv
charts/
  full_universe_telegram_pnl_over_time.png
```

## NPZ setup

The large NPZ is not included. Reuse the four previously uploaded parts:

```text
telegram_signals_1m_event_windows_bingx.npz.part001
telegram_signals_1m_event_windows_bingx.npz.part002
telegram_signals_1m_event_windows_bingx.npz.part003
telegram_signals_1m_event_windows_bingx.npz.part004
```

Put them into the bundle root or `npz_parts_go_here/`, then run:

```bash
python reassemble_npz.py
```

Expected NPZ path:

```text
local_test_bundle/DB/telegram_signals_1m_event_windows_bingx.npz
```

Prior known SHA256 after reassembly:

```text
bdcfc5dfefccae05cfa17c9b36de9a15c2d07f38ef6252287c880f2b3b9a5b44
```

## Run official baselines

```bash
pip install -r local_test_bundle/requirements_local_test.txt
./run_official_baselines.sh
```

Expected rough baseline:

- TP2 50/50 + channel close: about -26% PnL, -27% MDD.
- fixed thirds + channel close: about -27% PnL, -28% MDD.

## Run experimental strategy versions

```bash
./run_research_versions.sh
```

This reruns the relative version of the research harness into:

```text
reports/strategy_versions_research_rerun/
```

## Warnings for next agent

1. Do not optimize on `raw_edge_min5` as if it were OOS. It is a filtered positive subset.
2. Do not trust no-SL recovery on event-window NPZ as final proof. Need continuous OHLCV.
3. Do not use 30x leverage at this stage. Current research target is 1x.
4. Always report realized PnL, MTM DD, frozen capital, underwater duration, liquidation count.
5. TP-scale by symbol+side should be walk-forward only; in-sample TP-scale is diagnostic upper bound, not proof.
6. Immediate no-DCA task: reconstruct `raw_edge_min5`, split features into signal-time-known vs future-outcome-leaky, and validate any replacement selector with time-split or walk-forward tests against the all-signal TP3 negative control.

## Recommended next test protocol

Build continuous OHLCV first, then test:

```text
1x_no_hard_sl_recovery:
  hard_sl = off as normal stop
  liquidation = on
  target = 0.25/0.5/0.75/1.0 * TP1
  DCA adds = 0/1/2
  total cap = 1.0/1.5/2.0x
  max hold = 7/14/30d
  no new signal on symbol if unrecovered position exists
```

Filters to test without obvious curve-fit:

```text
RR TP1/SL >= 1.0
zone_pct <= 8-10%
symbol+side walk-forward prior hit72 / lifecycle PnL only using past signals
shorts separated from longs
```

Main objective: improve PnL/MDD and reduce frozen-capital tail, not maximize absolute PnL.

## 2026-05-16 validated handoff update

New external handoff:

```text
obw_platform/meta_strategies/telegram_dca_mvp/external_handoffs/telegram_dca_handoff_2026-05-16/handoff_package
```

Local reproduction:

```text
edge_min5 + TP3 + edge_in_zone + move_meta_stop_after_tp
signals_total = 83
opened_trades = 68
PnL = +9.2276%
MDD = -1.1122%
PnL/MDD = 8.2967
report_dir = obw_platform/meta_strategies/telegram_dca_mvp/reports/edge68_reproduce_720h_tp3
```

Control run with the same execution rule on the full static Telegram universe:

```text
signals_total = 312
opened_trades = 251
PnL = -16.1257%
MDD = -20.1267%
PnL/MDD = -0.8012
report_dir = obw_platform/meta_strategies/telegram_dca_mvp/reports/all_signals_720h_tp3_edge_in_zone_events
```

Loop priority:

```text
H0_edge_min5_selector_validation:
  Explain and rederive raw_edge_min5 using only at-entry information.
  Required checks: time split / walk-forward, no MFE or future outcome leakage, symbol+side cohort breakdown.
  Do not tune DCA yet. First prove a non-leaky selector or lifecycle rule that preserves the positive subset edge.
```
