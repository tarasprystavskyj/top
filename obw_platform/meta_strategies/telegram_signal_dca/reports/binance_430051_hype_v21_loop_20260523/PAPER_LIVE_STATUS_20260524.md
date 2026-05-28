# VeronicaUA HYPE Paper-Live Status

Paper/backtest-only. No live orders. No secrets.

- Updated: `2026-05-24T05:08:26.151398Z`
- State: `/var/www/vps2.happyuser.info/top/top_1/obw_platform/meta_strategies/telegram_signal_dca/reports/binance_430051_hype_v21_loop_20260523/veronicaUA_follow_open_state.json`
- Telemetry JSONL: `/var/www/vps2.happyuser.info/top/top_1/obw_platform/meta_strategies/telegram_signal_dca/reports/binance_430051_hype_v21_loop_20260523/veronicaUA_slippage_telemetry.jsonl`
- Open positions: `1`
- Closed trades: `1`
- Closed paper PnL: `3.1091725770899803` USDT
- Open unrealized PnL after configured exit slippage: `0.7679188099257273` USDT
- Combined paper PnL mark-to-exit model: `3.8770913870157075` USDT
- Slippage model in daemon: fixed `9.38` bp per entry/exit side
- Fee model in daemon: none; paper PnL currently does not deduct exchange fees.

## Open Positions

- `HYPEUSDT` `LONG` key `veronicaUA_follow_open:0_HYPEUSDT_BOTH:HYPEUSDT:LONG`
  - entry mark `60.075`, entry exec `60.13135035000001`, current mark `60.65` from `bingx_ccxt`
  - notional `100.0`, hypothetical exit exec `60.5931103`
  - unrealized PnL after configured exit slippage `0.7679188099257273` USDT / `0.7679188099257273`%
  - signal entry `59.96`, signal mark `60.093`, lead raw unrealized `0.30191000`
  - orderbook proxy `{'source': 'bingx_ccxt', 'best_bid': 60.632, 'best_ask': 60.668, 'mid': 60.65, 'spread_bp': 5.935696619950761, 'bid_depth_top5_usdt': 254388.3816127, 'ask_depth_top5_usdt': 18111.4676765}`

## Latest Closed Trade

- `{'key': 'veronicaUA_follow_open:0_HYPEUSDT_BOTH:HYPEUSDT:LONG', 'symbol': 'HYPEUSDT', 'side': 'LONG', 'detected_utc': '2026-05-23T21:43:40.702748Z', 'exit_detected_utc': '2026-05-24T01:01:21.862449Z', 'entry_mark_price': 58.163, 'entry_exec_price': 58.217556894000005, 'exit_mark_price': 60.084, 'exit_exec_price': 60.027641208000006, 'entry_slippage_bp_realized': 9.380000000001054, 'exit_slippage_bp_realized': 9.379999999999944, 'paper_pnl_usdt': 3.1091725770899803, 'paper_return_pct': 3.1091725770899803, 'exit_reason': 'lead_position_no_longer_open'}`
