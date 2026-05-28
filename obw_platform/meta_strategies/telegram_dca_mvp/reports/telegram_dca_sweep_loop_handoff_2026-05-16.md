# Telegram DCA Sweep Loop Handoff - 2026-05-16

Mode: research-only, paper/static backtests only. Do not touch live execution, daemons, broker/order code, secrets, deploy state, or paper-live processes.

## Current Loop State

- Watchdog target session has been moved to Jason: `019e3182-5823-7201-b156-097511a3a30a`.
- Telegram consilium config now points at `telegram_exec_reviewer` only, with DCA research allowed.
- Last completed supervisor cycle observed: `cycle_000021`.
- `cycle_000021` was productive but the web response was short/truncated and did not produce actionable DCA sweep instructions.
- `next_cycle_plan.json` still reports generic `stale_blocker_signature`, so the loop needs this concrete research direction.

## Hard Guardrails

- Paper/static replay only.
- No live execution, no daemon changes, no broker/order path, no secrets.
- Do not promote anything toward live.
- Do not treat `raw_edge_min5` or `raw_and_dca_edge_min5` as causal filters until they pass walk-forward validation.

## Key Evidence Already Known

Full universe controls are weak/negative. The positive edge is concentrated in selected symbol subsets.

Oracle-like selected symbols from `raw_edge_min5`:

```text
AAVE, ADA, APT, DOT, ENS, JUP, NEAR, OP, ORDI
```

Known toxic or large negative full-universe symbols to ablate or isolate:

```text
RENDER, SUI, INJ, XRP, LINK, PYTH, GALA, FET, LDO, JTO, ONDO, ROSE, ICP, VET
```

Observed DCA snapshot on `raw_and_dca_edge_min5`:

| adds | total mult | trades | MTM PnL | MTM MDD | PnL/MDD |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 1.5 | 51 | +6.8721% | -2.0758% | 3.3107 |
| 2 | 2.0 | 51 | +7.6195% | -2.5870% | 2.9453 |
| 3 | 2.5 | 51 | +10.4526% | -3.0611% | 3.4147 |

These are useful as a positive oracle benchmark, not as proof of deployable edge.

## Next Loop Mission

Run a bounded parallel paper-only DCA/filter research batch, then summarize results into CSV/JSON/MD artifacts under:

```text
obw_platform/meta_strategies/telegram_dca_mvp/reports/dca_sweep_2026_05_16_<slug>
```

The batch should compare:

1. Full universe: all static Telegram signals.
2. Oracle controls: `raw_edge_min5`, `raw_and_dca_edge_min5`.
3. Toxic ablations: full universe minus the toxic-symbol list.
4. Per-symbol and side cohorts: only symbols with enough count, separated by LONG/SHORT if supported by the signal CSV.
5. Causal candidate filters only:
   - signal geometry: zone width, SL distance, TP1 distance, TP1/SL RR;
   - entry realization: close-in-zone vs touch-zone vs first-bar;
   - time buckets known at signal time;
   - rolling prior-only symbol allow/deny list by chronological walk-forward;
   - early invalidation filters such as SL touched before valid entry.

## DCA Parameter Grid

Keep this grid small enough for one supervisor cycle or one explicit worker batch:

```text
meta_dca_adds: 0, 1, 2, 3
meta_dca_total_notional_mult:
  adds=0 -> 1.0
  adds=1 -> 1.25, 1.5
  adds=2 -> 1.5, 2.0
  adds=3 -> 2.0, 2.5
exit_at_tp: 1, 2, 3
tp_margin_weights: edge_in_zone, 0.5,0.5,0, 0.33,0.33,0.34
entry_mode: close_in_zone, touch_zone
signal_ttl_hours: 24, 72
signal_hard_ttl_sec: 3600
move_meta_stop_after_tp: true
ignore_lower_exits: true
```

## Command Template

Use separate output dirs per run. Do not overwrite existing baseline artifacts.

```powershell
python obw_platform\meta_strategies\telegram_dca_mvp\run_telegram_dca_mvp_npz.py `
  --npz DB\telegram_signals_1m_event_windows_720h_bingx.npz `
  --signals-csv <candidate_signals_csv> `
  --events DB\telegram_signal_standard_bt\telegram_channel_exit_events.csv `
  --out-dir obw_platform\meta_strategies\telegram_dca_mvp\reports\dca_sweep_2026_05_16_<slug> `
  --entry-mode <close_in_zone|touch_zone> `
  --signal-ttl-hours <24|72> `
  --signal-hard-ttl-sec 3600 `
  --exit-at-tp <1|2|3> `
  --tp-margin-weights <edge_in_zone|0.5,0.5,0|0.33,0.33,0.34> `
  --move-meta-stop-after-tp `
  --ignore-lower-exits `
  --meta-dca-adds <0|1|2|3> `
  --meta-dca-total-notional-mult <mult> `
  --load-only-signal-symbols
```

## Acceptance Gates

A candidate is only worth keeping if it is not future-leaky and meets all of:

- at least 40 opened trades, or explicitly marked as a thin per-symbol diagnostic;
- positive MTM PnL on out-of-sample or chronological walk-forward segment;
- drawdown not materially worse than the corresponding no-DCA execution;
- result survives removing the single best symbol;
- result includes a negative control versus full-universe or toxic-symbol set.

## Required Output

Each loop batch should write:

- `sweep_manifest.json`: exact commands, inputs, git branch/commit if available, timestamp.
- `sweep_results.csv`: one row per run with filter, adds, mult, TP, entry mode, TTL, trades, PnL, MDD, PnL/MDD.
- `symbol_side_breakdown.csv`: PnL/MDD/count by symbol and side where possible.
- `filter_leakage_audit.md`: which inputs are known at signal time, which are oracle/future-leaky.
- `next_recommendation.md`: keep/drop/needs-walk-forward for each candidate.

## Immediate Next Action For Loop

Start with a small parallel batch:

1. Full universe, no toxic symbols, `close_in_zone`, TP2, adds 0/1/2/3.
2. `raw_and_dca_edge_min5`, `close_in_zone`, TP2, adds 0/1/2/3 as oracle benchmark.
3. Full universe minus toxic symbols, `touch_zone`, TP2, adds 0/1/2/3.
4. Walk-forward prior-only symbol allowlist prototype using chronological trades; do not use full-sample symbol PnL.

Summarize before expanding the grid.
