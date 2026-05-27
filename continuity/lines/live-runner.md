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

## 2026-05-27 Veronika Worker Note

- Branch `codex/veronika-live-runner-sep-20260527` adds a small `obw_platform/runners/strategy_intents.py` contract and routes `live_runner_dual.py` entry/manage decisions through strategy-owned intents.
- Runner still owns exchange mechanics, order normalization, slippage telemetry, state persistence, and reconciliation; strategy remains the source for open, close, partial close, DCA, and retry/backoff permission.
- Remaining drift risk: `live_runner_dual.py` is still the large canonical execution file; a later pass should split exchange adapter/telemetry helpers from the bar loop after scenario coverage is broader.

## 2026-05-27 Continuation Safety Note

- Limit-to-market fallback is now blocked unless strategy intent/policy explicitly allows fallback and the matching reject/timeout reason.
- Entry signals without `qty` no longer silently inherit runner notional sizing unless they declare `sizing_policy=delegate_notional`.
- DCA order style now comes from strategy intent or strategy execution policy for legacy state-delta DCA; the runner no longer chooses DCA market vs limit from `_pending_entry_order_type` in the active DCA path.
- Focused runnable tests live in `obw_platform/tests/test_live_runner_strategy_intents.py`. This is still not live-restart-ready because full replay coverage, pending limit persistence, and observability consistency gates remain incomplete.

## 2026-05-27 Continuation Restart Note

- Pending limit entries are now persisted to `live_pending_entries.json` and reloaded on live startup.
- Startup now checks exchange open orders against persisted pending entries and blocks if it sees untracked non-reduce-only open orders.
- The stdlib test suite covers restart-like pending fill sync, untracked-open-order blocking, STOP/KILL entry/DCA blocking with close still allowed, direct-submit backoff blocking, and DB telemetry consistency.
- `cryptomine_pack_dual_full_lifo_strict` now exposes the same execution policy/backoff/min-order hooks as the main V21 strategy class.
- Lagrange follow-up fixed three restart/risk blockers: stale pending entries fetch/apply final fill state before cancel/pop, entry/DCA backoff no longer blocks TP/SL/reduce closes, and restart guard prefers exchange-wide open-order inspection before symbol-scoped fallback.
