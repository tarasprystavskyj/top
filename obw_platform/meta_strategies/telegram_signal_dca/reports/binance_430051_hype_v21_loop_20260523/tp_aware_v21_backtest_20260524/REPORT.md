# HYPE TP-Aware V21 Backtest

Research/paper only. No live orders, no secrets, no network.

## Purpose

This backtester fixes the known limitation of the closed-position replay: Binance public open positions are treated as entry signals, but exits are produced by a local V21/Pine-like engine instead of `avgClosePrice` from the Binance lead close.

## Implemented Behavior

- Lead side is authoritative; this report filters to `HYPEUSDT LONG` and does not flip or infer side.
- Entry time and first entry price come from Binance open position history.
- Compound sizing uses the canonical `$500` target capped by current realized equity.
- DCA uses champion shape `t500_b16_s0p25-0p35-0p55_w0p8-1p2-2p2` with close-confirmed adverse levels.
- Full TP uses `tpPercent=0.52` and `callback=0.1`.
- Sub-sell logic is implemented with `subSell=0.65`, but the champion `marginCallLimit=4` means `numBuys > 5` is never reached in this default lane.
- Fees and slippage use the same research defaults as the grounded compound replay.

## Missing / Approximate Behavior

- TradingView order scheduling, `allowThisBar` parity anchor, and 3-minute throttle are not fully modeled.
- Intrabar event order is approximated with OHLC and close confirmation; this is not tick replay.
- One signal is simulated as one independent warehouse. Overlapping live warehouses are not merged.
- `tpPercent`, `callback`, and `subSell` are now modeled, but this is still a research simulator that needs paper-live validation.

## First Verification

- Positions: `122`.
- Window: `2026-01-09T19:16:26.280000Z` .. `2026-05-24T01:01:12.362000Z`.
- Initial equity: `$500.00`.
- Finish equity: `$671.63`.
- Net: `34.33%`.
- PF: `inf`.
- Max realized DD: `0.00%`.
- Max MTM DD: `-1.77%`.
- Min trade MTM: `-1.94%`.
- Avg DCA fills: `0.71`.
- Avg sub-sells: `0.00`.
- Max notional: `$500.00`.
- `notional > equity_before`: `0`.
- Exit reasons: `{"full_tp_trailing": 122}`.

## Interpretation

These results are not directly comparable to the previous `$500 -> $2109.57` champion result because that replay closed at Binance lead close. This report is the first TP-aware lane for tuning `tpPercent`, `callback`, and `subSell` without pseudo-optimizing unused fields.

PF is `inf` in this first run because every simulated realized trade closed positive. Treat that as a model diagnostic, not a promotion signal: the current OHLC-level trailing approximation can be optimistic until validated against paper-live fills and a stricter intrabar event-order model.
