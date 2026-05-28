# HYPE ie500 TradingView Copy Local Backtest 90d

Research-only. No live orders, no secrets, no network calls.

## Source

- Pine source: `C:\python_scripts\top_1\obw_platform\strategies\pine\C - LONG - MA driven HYPE ie500 fixed champion - копія.pine`
- Local OHLC: `C:\python_scripts\top_1_dev_veronica\obw_platform\meta_strategies\telegram_signal_dca\reports\binance_430051_hype_v21_loop_20260523\binance_4300516091842181632_hype_universe_1m_20250524_20260524.npz`
- Local emulator: `tv_hype_ie500_local_backtest.py`

The local file named with Ukrainian `копія` differs from `C - LONG - MA driven HYPE ie500 fixed champion.pine` only by final newline. Its TradingView result near `+114%` appears to come from the default `Manual / CSV pack` settings, not from the `rnd5337 HYPE FBC` preset.

## Config

Core strategy config extracted from Pine defaults:

- Initial capital: `500`
- Commission: `0.05%`
- Process orders on close: enabled in Pine; local emulator fills at bar close.
- Sizing: compound, `strategy.equity` style.
- Base order: `16%` of sizing equity.
- Max position cost: `100%` of sizing equity.
- TP: `0.52%`
- Trailing callback: `0.10%`
- Max buys: `4` total.
- DCA drops: `[0.25, 0.35, 0.55, 3.00, 4.00]`
- DCA multipliers: `[1.0, 1.5, 2.75, 1.5]`
- Max DCA fills per bar: `2`
- DCA trigger: high/low touch plus close-confirm below level.
- Sub-sell TP: `0.65%`, breakeven close confirmation.
- Hard DD stop: `-50%`.
- Order throttle: max `6` orders per 3 minutes.
- Parity anchor: every second 1m bar from UTC Jan 1, 2026.

Full machine-readable config is in `config.json`.

## Local 90d Result

Window:

- Start: `2026-02-23T02:58:00Z`
- End: `2026-05-24T02:58:00Z`
- Bars: `129601`

Metrics:

| Metric | Value |
|---|---:|
| Start equity | 500.000000 |
| End equity | 1065.444076 |
| Net pct | 113.088815 |
| Max equity | 1096.193105 |
| Max drawdown pct | -19.889437 |
| Min total PnL pct | -6.829168 |
| Realized PnL | 680.730144 |
| Unrealized PnL | -25.842530 |
| Commission paid | 89.443538 |
| Orders | 755 |
| First buys | 211 |
| DCA buys | 334 |
| Full TP closes | 210 |
| Sub-sells | 0 |
| Hard DD stops | 0 |
| Max orders per bar | 2 |
| Max orders per 3m | 2 |
| Open position cost | 1081.968936 |
| Open position avg | 61.612600 |

## Interpretation

The local emulator result `+113.09%` is close enough to the reported TradingView `~114%` to treat this as the same strategy/config family for research.

This is not an exact TradingView broker emulator export. Remaining parity differences can come from TradingView's internal commission/equity timing, bar-window start, visible range boundaries, and whether open PnL is included exactly the same way.

## Files

- `strategy.pine`: snapshot of the TradingView copy used for this local parity run.
- `config.json`: extracted strategy config.
- `summary.json`: local backtest summary.
- `equity_curve.csv`: per-bar equity and position state.
- `orders.csv`: local order/fill event log.
