# Akela Meta Short Agent State

Updated: 2026-05-12 UTC

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

## Data Collection Plan

The loop should now try to build missing 1m 1y datasets for the first promoted
portfolio-router candidates:

- `IDOL`
- `FREEDOMMONEY`
- `MAXXING`
- `SUP`

Known existing 1y files:

- `DB/fast_cache_1m_freedommoney_1y_bingx.npz`
- `DB/fast_cache_1m_maxxing_1y_bingx.npz`

Missing files should be fetched by the iteration script with the existing,
trusted fetcher:

```bash
python3 obw_platform/scripts/fetch_backfill_ohlcv_npz_from_now_v1.py \
  --input-csv obw_platform/meta_strategies/akela_meta_short/data/universe_<symbol>_1m_1y.txt \
  --timeframe 1m \
  --back-bars 525600 \
  --exchange bingx \
  --ccxt-symbol-format usdtm \
  --npz-out DB/akela_meta_short_1m_1y_<symbol>_bingx.npz \
  --feature-set none \
  --cache-pack-trend
```

If a fetch fails, the loop records the failure under
`_reports/akela_meta_short/data_collection_state.json` and waits before
retrying. This avoids a sterile retry loop if the exchange or VPS IP blocks the
request.

Current data status after the 2026-05-12 morning run:

- `IDOL`: `DB/akela_meta_short_1m_1y_idol_bingx.npz`, 482080 bars.
- `FREEDOMMONEY`: `DB/fast_cache_1m_freedommoney_1y_bingx.npz`, 104184 bars.
- `MAXXING`: `DB/fast_cache_1m_maxxing_1y_bingx.npz`, 108569 bars.
- `SUP`: `DB/akela_meta_short_1m_1y_sup_bingx.npz`, 251806 bars.

Since all first-basket yearly datasets are now present, the loop must treat
more proxy-only reruns as low-value. The next useful move is basket validation.

## Basket Validation Plan

Goal: prove or falsify that the upper Akela selector improves a lower short leg
on a small multi-symbol portfolio.

Use only existing backtesters and configs. Do not create new exchange,
slippage, fee, liquidation, or portfolio accounting math.

Candidate basket:

- `IDOL/USDT:USDT`
- `FREEDOMMONEY/USDT:USDT`
- `MAXXING/USDT:USDT`
- `SUP/USDT:USDT`

Initial baseline config for smoke validation:

- `obw_platform/configs/V21_freedommoney_bingx_live_candidate_1m_1y.yaml`

Backtester:

- `obw_platform/backtester_dual_long_short_fast_pack_v2.py`

For each symbol, run a per-symbol backtest using the matching NPZ and explicit
`--symbol`. Write outputs under `_reports/akela_meta_short/basket_<stamp>/`.
Export curves with `--export-curves` and save all stdout logs.

After per-symbol backtests succeed, build a basket report that aggregates only
reported outputs from the existing backtester:

- per-symbol MTM return;
- per-symbol MTM drawdown;
- trades;
- margin calls if present;
- final unrealized/tail exposure if present;
- equal-weight basket MTM curve if compatible curves are available.

If any symbol fails, record the failure and keep the basket report partial. A
partial, honest report is better than a silent skip.

## Evidence Gates

- Prefer rolling-window validation over a single full-period rank.
- Track repeated winners across phase rank, monthly rank, and short-leg rank.
- Treat a symbol as a candidate only if it appears in more than one independent
  report or has a clear reason to be investigated.
- Do not accept a candidate solely because one proxy score is high.
- Do not change backtest math, slippage math, fee math, exchange emulation, or
  liquidation behavior without asking first.

## Next Steps

- Run basket/portfolio backtests for `IDOL`, `FREEDOMMONEY`, `MAXXING`, and
  `SUP`.
- Promote only stable selector logic to code.
- Keep V21 live continuation separate until this router has enough evidence.
