# Akela Meta Short

Akela Meta Short is a research lane for an upper-level multi-symbol router.
The lower layer is an existing short-leg strategy. The upper layer tries to
identify symbols and market regimes where the short leg has a structural edge:
post-hype decay, failed leadership, late-cycle distribution, and slow capital
rotation into stronger competitors.

The immediate goal is not to replace V21. It is to build a portfolio selector
that can eventually tell a proven short leg which symbols to trade, when to
stand down, and how much risk to allocate.

## Current Architecture

- `akela_meta_iteration.py` runs one deterministic research iteration.
- `akela_basket_validation.py` runs the first yearly candidate basket through
  the trusted V21 short-leg backtester.
- `run_worker_loop.sh` runs iterations in a supervised loop.
- `NEXT_WORKER_PROMPT.md` documents the evidence gates for a future LLM worker.
- `AGENT_STATE.md` is the handoff state for humans and agents.
- `reports/` stores small markdown/json summaries that are safe to commit.
- raw CSV/JSON/log artifacts are written to `_reports/akela_meta_short/`.

The legacy research scripts remain in `obw_platform/` for compatibility:

- `akela_research_cache_builder_v2.py`
- `rank_fast_cache_akela_phase_proxybt.py`
- `monthly_akela_phase_proxybt.py`
- `rank_short_leg_all_symbols_akela_v2.py`

Each iteration currently runs three selector profiles:

- `baseline`: current defaults from the existing scripts.
- `sensitive_failed_pump`: earlier failed-pump detection.
- `strict_late_decay`: slower, stricter late-decay confirmation.

## Worker Modes

The worker defaults to the legacy proxy/ranking loop:

```bash
./obw_platform/meta_strategies/akela_meta_short/run_worker_loop.sh
```

To validate the first candidate basket instead:

```bash
OBW_AKELA_LOOP_MODE=basket ./obw_platform/meta_strategies/akela_meta_short/run_worker_loop.sh
```

The basket mode is still read/report orchestration. It uses
`obw_platform/backtester_dual_long_short_fast_pack_v2.py` with explicit
`--npz` and `--symbol`, writes raw artifacts under `_reports/akela_meta_short/`,
and commits only compact summaries under this lane.

## Margin-Zero Codex Loop

The optional clean-slate Codex loop searches for V21 parameter variants that
keep yearly candidate backtests at `margin_call_events_total = 0`.

```bash
tmux new-session -d -s akela_margin_zero_codex \
  -c /var/www/vps2.happyuser.info/top/top_1 \
  './obw_platform/meta_strategies/akela_meta_short/run_margin_zero_codex_loop.sh'
```

It starts a new `codex exec` each cycle using
`MARGIN_ZERO_CODEX_PROMPT.md`. Experimental configs stay under
`generated_configs/margin_zero/`; raw logs stay under
`_reports/akela_meta_short/margin_zero_codex_loop/`.

Runtime retention is automatic for raw artifacts:

- basket validation keeps the newest raw basket directories and removes older
  `_reports/akela_meta_short/basket_*` directories after their committed
  summaries exist;
- margin-zero Codex keeps only the newest Codex loop logs and skips a cycle
  when free disk is below `OBW_MARGIN_ZERO_CODEX_MIN_FREE_MB`.

Do not auto-delete generated YAMLs or compact reports just because a branch was
rejected. Those are decision history and should be removed only after human
confirmation.

## Research Contract

Every candidate must pass two levels of proof before being treated as useful:

1. The upper layer finds recurring symbols or phases across time windows.
2. The lower short leg confirms that those selections produce better risk
   adjusted performance than naive shorting or buy-and-hold comparison.

Do not introduce a new exchange model, fee model, slippage model, liquidation
model, or backtest math without explicit human approval. The project currently
trusts the existing backtesters and tuners because they were iterated over many
rounds.

## Branch Policy

This lane lives on branch `akela-meta-short-worker`.
The loop may auto-commit stable files under this directory only. It must not
stage or commit unrelated worktree changes.
