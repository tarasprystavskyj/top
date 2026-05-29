# Telegram DCA Parallel Sweep Cycle 001 Findings

Date: 2026-05-16
Mode: paper-only research
Live readiness: false

## Scope

This cycle executed a bounded paper-only DCA sweep for static Telegram signals.

Files:
- Request: `obw_platform/meta_strategies/telegram_dca_mvp/reports/dca_parallel_sweeps/DCA_PARALLEL_SWEEP_CYCLE_001_REQUEST.json`
- Runner: `obw_platform/meta_strategies/telegram_dca_mvp/run_telegram_dca_mvp_npz.py`
- NPZ: `DB/telegram_signals_1m_event_windows_720h_bingx.npz`
- Signals: `DB/telegram_signal_standard_bt/telegram_signals_extracted.csv`
- Events: `DB/telegram_signal_standard_bt/telegram_channel_exit_events.csv`
- Output root: `obw_platform/meta_strategies/telegram_dca_mvp/reports/dca_parallel_sweeps/cycle_001`

Safety:
- No live execution touched.
- No paper-live daemon touched.
- No broker/order/secrets/deploy state touched.
- All runs are static paper backtests.

## Main Full-Universe Sweep

The initial wrapper timed out at the tool level, but child paper runs completed. A separate aggregation pass deduplicated by effective runner parameters.

Artifacts:
- `sweep_summary_unique_params.csv`
- `top20_unique_by_pnl.csv`
- `top20_unique_by_pnl_mdd.csv`

Coverage:
- Present run artifacts: 109
- Unique parameter sets: 81
- Main branch: `all_49`
- Signals total per run: 312
- Opened signals per run: 256

Best full-universe run by PnL:

```text
run_id: all_49__adds1__cap2x__tp1__wedge
meta_dca_adds: 1
meta_dca_total_notional_mult: 2.0
exit_at_tp: 1
tp_margin_weights: edge_in_zone
opened_signals: 256
mtm_pnl_pct: -9.8047%
mtm_mdd_pct: -10.2825%
mtm_to_mdd: -0.9535
dca_fill_count: 21
```

Interpretation:
- No full-universe DCA parameter family crossed positive.
- Increasing DCA capacity improved loss versus several weaker settings, but did not create a durable edge.
- TP1 exit dominated the least-bad full-universe variants.

## Diagnostic Filter Sweep

Artifacts:
- `commands_filters.ps1`
- `filter_runs/filter_sweep_summary.csv`
- `filter_runs/filter_sweep_summary.json`

Coverage:
- Diagnostic branches: 8
- Runs per branch: 4
- Completed diagnostic runs: 32

Best branch results by PnL:

```text
toxic_symbols_only:
  best_run: toxic_symbols_only__base_adds0_cap1_tp1_edge
  signals: 62
  opened: 51
  mtm_pnl_pct: -0.1401%
  mtm_mdd_pct: -1.9826%
  mtm_to_mdd: -0.0707

rr_tp1_sl_ge_1:
  best_run: rr_tp1_sl_ge_1__base_adds0_cap1_tp1_edge
  signals: 32
  opened: 27
  mtm_pnl_pct: -0.3969%
  mtm_mdd_pct: -2.3195%
  mtm_to_mdd: -0.1711

shorts_only:
  best_run: shorts_only__base_adds0_cap1_tp1_edge
  signals: 84
  opened: 68
  mtm_pnl_pct: -4.2360%
  mtm_mdd_pct: -5.0073%
  mtm_to_mdd: -0.8460

longs_only:
  best_run: longs_only__dca1_cap1p5_tp3_half
  signals: 228
  opened: 188
  mtm_pnl_pct: -5.7244%
  mtm_mdd_pct: -7.4582%
  mtm_to_mdd: -0.7675

no_toxic_symbols:
  best_run: no_toxic_symbols__dca2_cap2_tp2_thirds
  signals: 250
  opened: 205
  mtm_pnl_pct: -7.8097%
  mtm_mdd_pct: -7.9138%
  mtm_to_mdd: -0.9868
```

Interpretation:
- `rr_tp1_sl_ge_1` is too small for promotion in this cycle: 27 opened trades, below the 50-trade diagnostic gate.
- `toxic_symbols_only` is a damage-isolation cohort from prior handoff context, not a deployable selector. It is useful for diagnosis only.
- `no_toxic_symbols` improved against the main full-universe best PnL but remained negative.
- Zone filters `zone_pct_le_8` and `zone_pct_le_10` did not filter anything in this dataset; both retained 312 signals and matched all_49 behavior.

## Decision

Branch state: ACTIVE

Promotion:
- No run is promotable.
- No live or paper-live action is justified.
- No oracle/raw-edge selector should be treated as causal.

Next research direction:
- Stop broad all_49 DCA capacity sweeps for now; they are consistently negative.
- Narrow to causal filters that reduce damage without future leakage.
- Prioritize short-only, long-only, and RR geometry diagnostics, but require at least 50 opened trades for any promotion claim.
- Investigate why the prior toxic cohort is near-flat when isolated and why excluding it is still negative.
- Add per-symbol and symbol-side prior-only walk-forward filters, then rerun on non-oracle branches only.
