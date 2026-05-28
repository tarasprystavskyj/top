# Live Runner Dual Responsibility Map

Created: 2026-05-27

## Reference Shape

`obw_platform/bt_live_paper_runner_separated_universe_4.py` is the thin
wrapper reference:

- loads YAML/JSON config and records immutable config/code artifacts;
- merges config, file, and CLI universe allow/deny lists;
- hints restricted symbols into config/env;
- delegates to backtest, paper API, or live runner.

It does not implement strategy trading policy.

## Current Canonical Live Runner

`obw_platform/runners/live_runner_dual.py` remains the production live execution
adapter for the dual strategy path.

Runner-owned responsibilities preserved here:

- exchange symbol resolution and CCXT order submission;
- quantity/price precision, minimum-order normalization, and hedge/one-way
  exchange parameter handling;
- order fill polling, cancel confirmation, and rollback on failed execution;
- exchange position reconciliation;
- session DB/order/equity/PnL/slippage/debug telemetry persistence;
- live run manifest and strategy-state transition logging;
- market data fetch, feature cache load/save, prewarm, heartbeat, and stop guard
  checks.

Strategy-owned responsibilities now normalized through
`obw_platform/runners/strategy_intents.py`:

- open decision and requested TP/SL/order style from `entry_signal`;
- entry quantity must be explicit, or the signal must explicitly delegate notional
  sizing with `sizing_policy=delegate_notional`;
- limit-to-market fallback requires explicit per-intent or strategy execution
  policy permission (`allow_market_fallback` plus reject/timeout reason flag);
- close/TP/SL/partial close decision from `manage_position`;
- DCA/add decision and DCA order style from explicit manage action or
  strategy-owned execution policy for legacy state-delta DCA;
- retry/backoff/circuit permission through `can_submit_order`,
  `execution_backoff`, or `get_execution_backoff`;
- rejection/fill visibility through `on_order_rejected` and backoff hooks.

The runner may block execution for venue/control constraints such as
`STOP_NEW_ORDERS`, exchange rejection, missing exchange position, precision
normalization, or strategy-declared backoff. It should not invent a new open,
close, TP, SL, DCA, cooldown, or retry policy.

## Historical Live Files

`live_runner_dual_2.py`

- Earlier dual runner generation.
- Useful patterns: strategy snapshot/restore, rejection callback, fill sync,
  reduce-only close, fallback TP/SL.
- Covered by current `live_runner_dual.py`, which has those mechanics plus
  newer telemetry and reconciliation.
- No code copied in this pass.

`live_runner_dual_rebuilt.py`

- Compact rebuilt dual runner.
- Useful patterns: clearer `_execute_open_with_rollback`,
  `_execute_reduce_with_rollback`, `_maybe_apply_manage_result`, and
  `_attempt_entry` shape.
- Covered by this pass by extracting the intent normalization boundary while
  preserving the richer current live runner mechanics.
- No code copied in this pass.

`live_runner_dual_scenario_runner.py`

- Scenario harness for scripted strategy behavior against replay/virtual
  exchange.
- Preserved unchanged. It imports the same `_attempt_entry` and
  `_maybe_apply_manage_result` helpers and compiles after the intent refactor.
- Next useful step is adding concrete JSON scenario fixtures for HYPE open,
  DCA, TP, rejection, and retry/backoff flows.

## Current Boundary Checklist

`live_runner_dual.py`

- `_attempt_entry`: strategy signal is required; missing qty is skipped unless
  the strategy explicitly delegates sizing. Remaining TODO: record a visible
  skipped order/debug event for contract rejects.
- `_execute_open_with_rollback`: limit reject/timeout fallback now requires
  runner config plus strategy intent/policy permission. Remaining TODO:
  replace flat fallback booleans with a typed `ExecutionPolicy`.
- `_place_market_fallback_open`: direct fallback calls are guarded by
  `allow_market_fallback` and strategy backoff before market submit.
- `_maybe_apply_manage_result`: DCA order type now comes from the intent or
  strategy execution policy. Remaining TODO: migrate active strategies from
  state-delta DCA to explicit `ADD` return objects.
- `_execute_reduce_with_rollback`: close/partial/no-position sync remains
  runner-owned broker reconciliation. Remaining TODO: make `sync_if_absent`
  close semantics explicit in `ExecutionIntent`.
- `place_open_qty` / `place_reduce_only`: venue parameter retry remains
  runner-owned broker mechanics. Remaining TODO: expose retry policy in
  execution metrics.
- `_sync_pending_entry_orders`: DCA limit fill/cancel polling remains
  runner-owned broker mechanics. Pending entries are persisted to
  `live_pending_entries.json`; startup blocks when exchange open orders are not
  represented in that file. Stale pending entries now fetch/apply fill state
  before cancel/pop, then fetch/apply final state after cancel attempts.
  Remaining TODO: persist richer pending-order DB rows and recover strategy
  snapshots for no-fill rollback after restart.
- `_apply_dynamic_min_order_floor`: now prefers a strategy
  `set_broker_min_order_usdt` hook, falling back to legacy attribute mutation.
  TODO: remove the fallback after all live strategies implement the hook.
- `_strategy_restore` / `_strategy_sync_after_fill`: private `_states` fallback
  still exists for legacy strategies. TODO: require explicit strategy hooks for
  production live classes.

`strategy_intents.py`

- `ExecutionIntent` now includes fallback permission, sizing policy, and close
  semantics fields.
- `entry_intent_from_signal` no longer silently invents qty from runner
  notional.
- `manage_intent_from_result` requires DCA order style via explicit result or
  strategy-provided default policy.
- `StrategyRejectionBackoff` is strategy-owned and exportable. TODO: add
  import/round-trip and per-family counters for restart persistence.
- Close/partial submission uses a dedicated close permission path so
  risk-increasing entry/DCA backoff does not block TP/SL/reduce actions.

`strategies/cryptomine_pack_dual_full.py`

- Active V21 strategy now exposes execution policy and a per-symbol backoff.
- DCA order type is strategy-visible (`dcaOrderType`/`dca_order_type`, with
  legacy config migration fallback). TODO: add the same complete hooks to every
  live-candidate strategy before live restart.

`strategies/cryptomine_pack_dual_full_lifo_strict.py`

- Strict LIFO live-candidate strategy now exposes execution policy, per-symbol
  backoff, and broker min-order hooks.

## Known Remaining Risks

- `live_runner_dual.py` is still too large; exchange adapter, telemetry, and bar
  loop concerns are not physically split yet.
- Existing strategy classes still use mixed legacy patterns: some return
  explicit manage actions, while DCA often mutates strategy/position state.
- No live or paper-live process was started for this change. Restart requires a
  replay/scenario pass and human live-safety approval.
- `pending_entries` is memory-only. Restart-readiness must block unless the
  exchange confirms no open maker/limit orders, or pending orders are persisted
  and reconciled. This pass added JSON persistence and a startup guard, but not
  full DB-backed lifecycle accounting.
