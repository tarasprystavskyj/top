# Production Live Runner Plan

Target: make live execution close enough to paper-live that profitable paper
results are meaningful. Real orders must remain disabled until all gates below
pass.

## Current Base

`obw_platform/runners/live_runner_dual.py` already has useful production
building blocks:

- auth probe before live operation;
- session DBs and order DB persistence;
- open-position persistence and reconciliation helpers;
- exchange trace proxy;
- order book pretrade snapshots;
- fill-observation/slippage recording;
- limit-entry fallback controls;
- runtime debug events;
- startup/loop reconciliation switches;
- slip guard environment variables.

Do not fork this runner casually. The safer path is an adapter that feeds
copytrading/Telegram signals into an execution interface with the same
idempotency, state, tracing, and reconciliation rules.

## Required Gates Before Real Orders

1. `paper` mode is profitable for at least several weeks with full logs.
2. `shadow` mode runs with live market data and exchange auth but no orders.
3. `dry_order` mode validates symbol mapping, min quantity, precision, and
   post-only/market fallback decisions without submit.
4. `live_small` mode uses hard caps:
   - no withdrawal-capable API key;
   - IP whitelist;
   - max notional per position;
   - max total open notional;
   - max daily loss;
   - max slippage bp;
   - kill-switch file.
5. Every submitted order has a stable client order id derived from
   `source_id + symbol + side + action + leg`.
6. On restart, runner reconciles:
   - local state;
   - exchange open orders;
   - exchange positions;
   - recent fills.
7. No duplicate entry is possible after restart, network timeout, or partial
   exchange response.

## Live Parity Rules

- Paper entry price must use the same mark/execution model as live:
  current BingX mark plus configured slippage or observed order-book model.
- DCA sizing must use same max-cap schedule as paper.
- Live runner must record paper-equivalent theoretical fills next to actual
  fills so drift can be measured.
- Every live fill updates the slippage observation tables.
- If slippage exceeds configured `MAX_ENTRY_SLIP_BP` or `MAX_EXIT_SLIP_BP`,
  skip/fail the order.

## Minimal Adapter Shape

The adapter should consume normalized signals:

```json
{
  "source": "binance_copy|telegram",
  "source_id": "stable id",
  "mode": "follow_open|contrarian_on_close|telegram_dca",
  "symbol": "BTC/USDT:USDT",
  "side": "LONG",
  "event": "entry|exit|dca",
  "price_hint": 65000.0,
  "max_notional": 100.0,
  "dca_leg": 0,
  "created_at_utc": "..."
}
```

It should produce desired state:

```json
{
  "symbol": "BTC/USDT:USDT",
  "side": "LONG",
  "target_notional": 100.0,
  "reason": "signal entry/dca/exit",
  "client_order_id": "stable deterministic id"
}
```

`live_runner_dual.py` or a small exchange adapter then handles precision,
submission, polling, persistence, and reconciliation.

## First Implementation Step

Build a `shadow_live` runner that:

- reads `binance_online_copytrading/reports/state.json`;
- reads Telegram paper SQLite rows;
- converts open/close/DCA paper events into normalized desired orders;
- validates BingX symbol availability and min order sizes;
- writes `reports/shadow_orders.jsonl`;
- never calls `create_order`.

Only after shadow drift is acceptable should real order submission be added.
