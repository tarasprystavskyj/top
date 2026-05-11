# Akela Meta Short Agent State

Updated: 2026-05-11 UTC

## Objective

Build a productive overnight research loop for an upper-level multi-currency
router that selects symbols/regimes for the existing short leg.

The economic thesis:

- A failed market leader often enters a slow decay phase.
- If the router detects that regime, the short leg may outperform passive
  exposure and compensate for occasional wrong regime calls.
- We need evidence that the selector is stable across windows, not just one
  favorable backtest.

## Available Inputs

Primary existing Akela datasets:

- `DB/akela_top200_1m_30d.research_v2_2_no_cross.npz`
- `DB/akela_top200_1m_30d.db`
- `DB/fast_cache_akela_shortlist_1m_30d.npz`
- `DB/combined_cache_5m_5000_04.09.phase0_top100.research_v2_2_no_cross.npz`

Primary existing scripts:

- `obw_platform/rank_fast_cache_akela_phase_proxybt.py`
- `obw_platform/monthly_akela_phase_proxybt.py`
- `obw_platform/rank_short_leg_all_symbols_akela_v2.py`
- `obw_platform/akela_research_cache_builder_v2.py`

## Current Loop

Run from repo root:

```bash
./obw_platform/meta_strategies/akela_meta_short/run_worker_loop.sh
```

The loop writes raw artifacts to `_reports/akela_meta_short/` and compact
summaries to this directory's `reports/`.

Current wiring:

- phase proxy rank: `DB/fast_cache_akela_shortlist_1m_30d.npz`
- rolling monthly proxy rank: `DB/fast_cache_akela_shortlist_1m_30d.npz`
- structural short-leg rank: `DB/akela_top200_1m_30d.db`
- selector profiles: `baseline`, `sensitive_failed_pump`, `strict_late_decay`

## Evidence Gates

- Prefer rolling-window validation over a single full-period rank.
- Track repeated winners across phase rank, monthly rank, and short-leg rank.
- Treat a symbol as a candidate only if it appears in more than one independent
  report or has a clear reason to be investigated.
- Do not accept a candidate solely because one proxy score is high.
- Do not change backtest math, slippage math, fee math, exchange emulation, or
  liquidation behavior without asking first.

## Next Steps

- Let the tmux worker produce repeated summaries overnight.
- Inspect `reports/latest_summary.md` in the morning.
- Promote only stable selector logic to code.
- Keep V21 live continuation separate until this router has enough evidence.
