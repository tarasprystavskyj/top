# backtest-integrity

## Stage

plant

## What This Line Is

The trust boundary between backtest, TradingView, paper, and live. This line exists to prevent pretty charts from being mistaken for evidence.

## Current Shape

The project already tracks MTM-oriented variants, live debug bundles, validation UI, fees/slippage concerns, and backtest-vs-live comparison. The missing piece is strict discipline: every result must say exactly what data, engine, config, costs, and execution assumptions were used.

## What Matters

- Realized PnL and MTM equity must both be shown.
- MTM drawdown and realized drawdown are not interchangeable.
- Fees, slippage, and funding must be explicit.
- Trade count, turnover, and notional exposure must be reported.
- Backtest/live comparison must start from the same time, same inventory state, same config, and same strategy code version.
- If limit orders are enabled, the fill model must be stated. If they are disabled, the config must say so explicitly.

## Drift Risks

- Reporting annualized return without showing the drawdown path.
- Comparing live to a backtest with different start alignment.
- Using a cache built from different bars/ticks than the live session observed.
- Forgetting exchange minimum order size, hedge mode, or funding mechanics.
- Treating TradingView labels as proof of Python/live parity.
- Running a tuner on one assumption and live on another.

## Next Useful Move

Create a short validation checklist for each active strategy version: engine, config, data cache, time window, cost model, fill model, realized PnL, MTM MDD, realized MDD, fees, turnover, trades, margin calls.
