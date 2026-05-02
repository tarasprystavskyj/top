# live-runner

## Stage

plant

## What This Line Is

Live and paper execution layer: runner entrypoints, exchange sync, session logging, warmup bars, order placement, TP/SL fallback, debug bundles, and comparison against backtest.

## Current Shape

The repo contains several live/paper runner variants and wrappers, including root-level runner files and `obw_platform/runners/*`. There are also session artifacts such as `session.sqlite`, live reports, and cache-session databases. This means the real active runner can easily become ambiguous.

## What Matters

- The live runner must not be a second strategy.
- The runner must feed the same strategy interface as the backtester.
- Strategy must receive enough warmup history before the first tradable bar.
- Exchange position, open orders, fills, fees, and funding must be logged for forensic replay.
- Session metadata must include config hash, strategy/code hash, cache/data source, exchange, symbol, timeframe, and start time.

## Drift Risks

- One wrapper uses different sizing or TP/SL behavior than the backtester.
- A fallback loop changes trading behavior silently.
- Warmup history is missing, so the strategy starts blind.
- Paper/live sessions cannot be replayed because fills and order responses were not persisted.
- API errors are swallowed instead of becoming visible session events.
- Live starts at a different bar/inventory state than the comparison backtest.

## Next Useful Move

Name one canonical live entrypoint and document its I/O contract: inputs, warmup requirement, strategy call format, expected logs, and comparison backtest command.
