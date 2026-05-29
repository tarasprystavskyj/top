# HYPE Veronica Optimization Acceleration Plan

Research-only. This plan does not propose live orders, private-page scraping, secret reads, or paper/live launch. Exact replay remains authoritative until every accelerated engine passes equivalence.

## 1. Acceleration policy

The project has two different workloads:

1. **Exact replay / promotion evidence**
   - event simulation
   - DCA fill branching
   - strict fill mode
   - compounded equity
   - notional/equity grounding
   - MTM tail risk
   - row-level audit

2. **Search / meta-optimization**
   - candidate generation
   - early rejection
   - trial ranking
   - surrogate/failure prediction

The exact replay workload should be accelerated first with CPU/Numba, not CUDA. GTX 1070 can be useful later as a CUDA sandbox for surrogate models, but it is not the core exact replay engine.

## 2. Search-space compaction

Do not optimize ~100 YAML parameters directly. Split them into families.

### Frozen execution invariants

These are not search parameters:

- executable fill source: `next_bar_open`, `first_bar_close`, or paper-live BingX telemetry
- no Binance `avgCost` / `avgClosePrice` as promotion fills
- slippage model and calibration metadata
- min order gate
- strict fill mode
- unlevered grounding gate
- initial equity for the experiment contract

### Core DCA shape family

Use a compact vector:

```text
[target_notional_frac, base_frac, dca_count, step_1, step_growth, weight_1, weight_growth]
```

Generate arrays:

```text
step_i   = step_1 * step_growth^i
weight_i = weight_1 * weight_growth^i
```

This prevents the optimizer from fitting arbitrary noisy step/weight patterns.

### Exit family

Optimize separately:

```text
[tp_pct, sl_pct, trailing_callback_pct, ttl_hours]
```

Do not mix historical copied-close replay with autonomous exit replay in the same promotion lane.

### Signal/filter family

Optimize separately:

```text
[freshness_hours, max_signal_delay_minutes, volatility_filter, regime_filter, side_filter]
```

### Risk/sizing family

Optimize separately:

```text
[max_notional_pct_equity, max_open_positions, cooldown_after_loss_hours, daily_entry_cap]
```

## 3. Optuna/TPE + ASHA design

Recommended order:

1. Sobol/LHS/random exploration over compact DCA family.
2. Optuna TPE over the same compact family.
3. Successive halving / ASHA budgets:
   - budget 1: short segment or first N trades
   - budget 2: 90d or medium trade count
   - budget 3: full train segment
   - budget 4: validation segment
   - budget 5: frozen holdout only for top-N
4. Neighborhood plateau search around the current champion.
5. CMA-ES only after the search vector is compact and continuous.

Hard prune early if:

- `notional_gt_equity_before_count > 0` in unlevered grounded mode
- `margin_call_count > 0` in leveraged diagnostics
- `min_order_ok == false`
- `max_mtm_dd_pct` below gate
- `min_trade_mtm_pct_equity` below gate
- entry-source crosscheck breaks
- slippage stress breaks

## 4. Trial registry schema

Append JSONL rows to:

```text
obw_platform/meta_strategies/telegram_signal_dca/champion_registry.jsonl
```

Minimum schema:

```json
{
  "schema_version": "hype_trial_registry_v1",
  "created_utc": "...",
  "agent_id": "...",
  "branch": "...",
  "commit": "...",
  "universe_id": "hype_binance_copy_year_...",
  "dataset_hash": "...",
  "signal_hash": "...",
  "search_space_id": "dca_shape_v1",
  "candidate_id": "...",
  "params": {},
  "entry_source": "next_bar_open",
  "fill_mode": "close_beyond_skip_boundary",
  "slippage_bp_per_side": 4.25,
  "initial_equity": 500,
  "metrics": {
    "net_pct": 0,
    "pf": 0,
    "max_mtm_dd_pct": 0,
    "min_trade_mtm_pct_equity": 0,
    "notional_gt_equity_before_count": 0,
    "margin_call_count": 0
  },
  "gate": {
    "status": "REJECTED|CANDIDATE|HOLDOUT_PASS|PAPER_TELEMETRY_REQUIRED",
    "fail_reasons": []
  },
  "report_path": "..."
}
```

## 5. Split design

Do not treat in-sample metrics as validation. Use a frozen promotion holdout even if the project already has metric gates.

Recommended split by closed signals/trades, not just bars:

- train/exploration: first 50-60% of trades
- validation: next 20-25% of trades
- frozen holdout: final 20-25% of trades

Rules:

- agents may optimize on train/validation;
- holdout is opened only for top-N;
- after holdout failure, do not tune on holdout and call it fresh validation;
- report `split_manifest.json` and `universe_id` in every wave.

## 6. Surrogate layer

Use exact CPU replay to create a large trial table:

```text
params + market/signal features -> metrics + gate pass/fail
```

Then train:

- LightGBM/CatBoost/XGBoost failure predictor
- pass-probability model
- MTM risk model
- slippage-fragility model
- candidate family selector

PyTorch is optional later for MLP/1D-CNN/small Transformer experiments. Do not use it as the first optimizer.

## 7. GTX 1070 role

Can accelerate:

- small PyTorch surrogate experiments
- batched inference
- some LightGBM GPU experiments
- general CUDA sandbox work

Should not be relied on for:

- exact promotion replay
- strict fill verification
- row-level audit
- YAML-object search loops
- Python branch-heavy event simulation

Pascal/GTX 1070 needs pinned CUDA/PyTorch environment. Treat it as optional infrastructure, not optimizer architecture.

## 8. Pass/fail gates for this patch stage

### PERF_BASELINE pass

- `perf_baseline.json` exists
- `perf_baseline.prof` exists
- `perf_top30.txt` exists
- JSON includes:
  - total runtime
  - candidate count
  - avg ms per candidate
  - avg ms per position
  - timed `simulate_position`
  - timed `simulate_candidate_rows`
  - timed `summarize`
  - timed `write_csv`
  - position/trade count
  - max candle window length
  - Python version/platform

### NUMBA prototype pass

- Numba candidate summary matches Python reference within tolerance:
  - `equity_end` abs diff < `1e-6`
  - `net_pct` abs diff < `1e-6`
  - `max_mtm_dd_pct` abs diff < `1e-6`
  - `avg_dca_fills` abs diff < `1e-12`
  - `notional_gt_equity_before_count` exact
  - `margin_call_count` exact
- Required candidates:
  - `rnd5337...`
  - `plain_no_dca_t500`
  - `current_like_dca3_t500`
  - 100 random candidates if local data/time allows

If any equivalence check fails, the Numba engine is **REJECTED_FOR_SEARCH** and may only be used for debugging.

## 9. Exact next commands for BinanceCopyOps1

Assume current working tree root is `C:\python_scripts\top_1_dev_veronica`.

### Apply overlay

```powershell
# unzip patch overlay into the repository root
Expand-Archive -Force hype_veronica_acceleration_overlay_20260528.zip C:\python_scripts\top_1_dev_veronica
cd C:\python_scripts\top_1_dev_veronica
```

### PERF champion only

```powershell
python obw_platform\meta_strategies\telegram_signal_dca\tools\perf_baseline_hype_dca.py `
  --mode champion `
  --positions-csv obw_platform\meta_strategies\telegram_signal_dca\reports\binance_430051_hype_v21_loop_20260523\wave_002\position_refresh\position_history_normalized.csv `
  --npz obw_platform\meta_strategies\telegram_signal_dca\reports\binance_430051_hype_v21_loop_20260523\binance_4300516091842181632_hype_universe_1m_20250524_20260524.npz `
  --out-dir DEX_REPORTS_LOCAL\hype_perf\champion_only\search_out `
  --perf-out-dir DEX_REPORTS_LOCAL\hype_perf\champion_only\perf `
  --entry-source next_bar_open `
  --slippage-bp 4.25 `
  --position-sizing-mode compound `
  --max-target-notional 500
```

### PERF 100 candidates

```powershell
python obw_platform\meta_strategies\telegram_signal_dca\tools\perf_baseline_hype_dca.py `
  --mode sample100 `
  --positions-csv obw_platform\meta_strategies\telegram_signal_dca\reports\binance_430051_hype_v21_loop_20260523\wave_002\position_refresh\position_history_normalized.csv `
  --npz obw_platform\meta_strategies\telegram_signal_dca\reports\binance_430051_hype_v21_loop_20260523\binance_4300516091842181632_hype_universe_1m_20250524_20260524.npz `
  --out-dir DEX_REPORTS_LOCAL\hype_perf\sample100\search_out `
  --perf-out-dir DEX_REPORTS_LOCAL\hype_perf\sample100\perf `
  --entry-source next_bar_open `
  --slippage-bp 4.25 `
  --position-sizing-mode compound `
  --max-target-notional 500
```

### PERF 1000 candidates, bounded

```powershell
python obw_platform\meta_strategies\telegram_signal_dca\tools\perf_baseline_hype_dca.py `
  --mode sample1000 `
  --positions-csv obw_platform\meta_strategies\telegram_signal_dca\reports\binance_430051_hype_v21_loop_20260523\wave_002\position_refresh\position_history_normalized.csv `
  --npz obw_platform\meta_strategies\telegram_signal_dca\reports\binance_430051_hype_v21_loop_20260523\binance_4300516091842181632_hype_universe_1m_20250524_20260524.npz `
  --out-dir DEX_REPORTS_LOCAL\hype_perf\sample1000\search_out `
  --perf-out-dir DEX_REPORTS_LOCAL\hype_perf\sample1000\perf `
  --entry-source next_bar_open `
  --slippage-bp 4.25 `
  --position-sizing-mode compound `
  --max-target-notional 500
```

### Numba equivalence smoke

```powershell
python obw_platform\meta_strategies\telegram_signal_dca\tools\numba_equivalence_check_hype_dca.py `
  --positions-csv obw_platform\meta_strategies\telegram_signal_dca\reports\binance_430051_hype_v21_loop_20260523\wave_002\position_refresh\position_history_normalized.csv `
  --npz obw_platform\meta_strategies\telegram_signal_dca\reports\binance_430051_hype_v21_loop_20260523\binance_4300516091842181632_hype_universe_1m_20250524_20260524.npz `
  --out-json DEX_REPORTS_LOCAL\hype_perf\numba_equivalence\equivalence_core_candidates.json `
  --entry-source next_bar_open `
  --slippage-bp 4.25 `
  --position-sizing-mode compound
```

### Numba equivalence with 100 random candidates

```powershell
python obw_platform\meta_strategies\telegram_signal_dca\tools\numba_equivalence_check_hype_dca.py `
  --positions-csv obw_platform\meta_strategies\telegram_signal_dca\reports\binance_430051_hype_v21_loop_20260523\wave_002\position_refresh\position_history_normalized.csv `
  --npz obw_platform\meta_strategies\telegram_signal_dca\reports\binance_430051_hype_v21_loop_20260523\binance_4300516091842181632_hype_universe_1m_20250524_20260524.npz `
  --out-json DEX_REPORTS_LOCAL\hype_perf\numba_equivalence\equivalence_100_random.json `
  --entry-source next_bar_open `
  --slippage-bp 4.25 `
  --position-sizing-mode compound `
  --include-random 100
```

## 10. Do not promote from this task

This patch only measures and prototypes acceleration. It does not produce a trading candidate, paper-live authorization, or live-readiness result.
