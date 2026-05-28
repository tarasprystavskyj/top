# HYPE Veronica Acceleration Overlay 2026-05-28

This overlay adds three research-only artifacts:

1. `obw_platform/meta_strategies/telegram_signal_dca/tools/perf_baseline_hype_dca.py`
2. `obw_platform/meta_strategies/telegram_signal_dca/experimental/numba_exact_replay_core.py`
3. `obw_platform/meta_strategies/telegram_signal_dca/tools/numba_equivalence_check_hype_dca.py`
4. `docs/optimization/HYPE_VERONICA_OPTIMIZATION_ACCELERATION_PLAN.md`

It does not change existing backtester files. It does not touch live orders, secrets, private account pages, or paper-live launch logic.

## Safety posture

- Existing Python replay is the reference engine.
- Numba prototype is rejected for search unless equivalence passes.
- GTX 1070 is treated only as optional CUDA/surrogate infrastructure.
- No live or paper-live promotion should be made from this patch.

## Smoke commands

See `docs/optimization/HYPE_VERONICA_OPTIMIZATION_ACCELERATION_PLAN.md`, section "Exact next commands for BinanceCopyOps1".

## Expected outputs

PERF baseline writes:

- `perf_baseline.json`
- `perf_baseline.prof`
- `perf_top30.txt`

Numba equivalence writes:

- `equivalence_core_candidates.json`
- optional `equivalence_100_random.json`

## Pass condition

The patch is useful only after local data-backed runs produce PASS outputs. Without local NPZ/CSV, this overlay is an implementation patch and cannot prove runtime speed or equivalence inside this slim archive.
