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
