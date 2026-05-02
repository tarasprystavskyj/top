# data-cache

## Stage

sprout

## What This Line Is

Data preparation and cache layer: DB/NPZ creation, tick-to-bar conversion, universe files, enrichment, compatibility conversion, and cache provenance.

## Current Shape

The repo contains many data builders, fetchers, converters, merge scripts, universe inputs, generated cache references, and research-enrichment workflows. This is necessary, but it creates a serious comparability risk when two runs use files with similar names but different construction assumptions.

## What Matters

- Every cache should have a manifest: exchange, market type, symbol format, timeframe, date range, source script, build date, and enrichment version.
- Raw, enriched, compressed, and compatibility-rewritten NPZ files must be named distinctly.
- A strategy benchmark must record the exact cache path used.
- Live calibration sessions need their own reproducible cache/session bundle.
- NumPy compatibility issues must be treated as data-contract bugs, not random environment problems.

## Drift Risks

- One candidate runs on OHLCV bars, another on reconstructed tick bars, but results are compared directly.
- Symbol formats differ between BingX, Bybit, Gate, and internal files.
- “Top-up” data silently changes a benchmark window.
- Research enrichment leaks future/cross-sectional information into the target window.
- Cache file names carry too much implicit meaning and no manifest.

## Next Useful Move

Add a minimal cache manifest convention and require every serious backtest/tuner output to print the cache path plus manifest fields.
