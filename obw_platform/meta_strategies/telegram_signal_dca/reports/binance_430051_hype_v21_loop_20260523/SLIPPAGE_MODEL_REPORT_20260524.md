# HYPE Dynamic Slippage Model Research

Paper/backtest-only sidecar report. This does not place orders.

- Updated: `2026-05-24T05:08:26.151398Z`
- Telemetry source: `/var/www/vps2.happyuser.info/top/top_1/obw_platform/meta_strategies/telegram_signal_dca/reports/binance_430051_hype_v21_loop_20260523/veronicaUA_slippage_telemetry.jsonl`
- Current daemon execution model: fixed configured slippage bp on entry and exit.
- Current daemon fee model: none in paper-live PnL.
- Current dynamic model status: `fallback_until_more_telemetry`.

## Inputs Collected

- Signal entry price and signal mark from Binance public open-position payload.
- Local simulated entry mark and entry exec price.
- Current public mark from BingX ccxt when available, Binance mark fallback otherwise.
- Configured slippage bp.
- Orderbook proxy when BingX orderbook is available: best bid/ask, spread bp, top-5 bid/ask depth.
- Unrealized PnL after applying configured exit slippage.
- Closed trade exit mark/exec/PnL from the existing state file.

## Current Fallback

- Use configured fixed slippage `9.38` bp until enough telemetry exists.
- If orderbook spread is available, provisional dynamic estimate is `max(configured_bp, spread_bp / 2)` for both entry and exit.
- This is deliberately conservative and only changes reporting/profitability estimation, not trading behavior.

## Next Data Needed

- More poll samples across volatile HYPE periods.
- Closed paper trades with entry/exit orderbook snapshots.
- Optional depth-at-notional impact estimate for 100 USDT and larger test notionals.

## Latest Snapshot

- Symbol: `HYPEUSDT`
- Current mark: `60.65` from `bingx_ccxt`
- Orderbook proxy: `{'source': 'bingx_ccxt', 'best_bid': 60.632, 'best_ask': 60.668, 'mid': 60.65, 'spread_bp': 5.935696619950761, 'bid_depth_top5_usdt': 254388.3816127, 'ask_depth_top5_usdt': 18111.4676765}`
- Suggested dynamic slippage: `{'status': 'fallback_until_more_telemetry', 'fallback_slippage_bp': 9.38, 'spread_half_bp': 2.9678483099753805, 'suggested_entry_slippage_bp': 9.38, 'suggested_exit_slippage_bp': 9.38}`
