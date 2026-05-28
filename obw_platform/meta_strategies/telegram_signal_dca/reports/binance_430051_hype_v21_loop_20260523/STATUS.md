# Binance 430051 HYPE V21 Loop

Paper/backtest-only. No live orders. No `.env` secrets are read or printed.

- Updated: `2026-05-24T04:50:00Z`
- tmux session: `binance_430051_hype_v21_loop`
- Lead: `4300516091842181632`
- Source URL: `https://www.binance.com/uk-UA/copy-trading/lead-details/4300516091842181632?timeRange=7D`
- Current phase: `sleeping_between_waves`
- Positions: `ok`
- Positions count: `122`
- Detected symbols: `HYPEUSDT`
- Detected sides: `{"LONG": 122}`
- Long-only assumption: `True`
- Position window: `2026-01-09T19:16:26.280000Z` .. `2026-05-24T01:01:12.362000Z`
- HYPE annual/window NPZ: `/var/www/vps2.happyuser.info/top/top_1/obw_platform/meta_strategies/telegram_signal_dca/reports/binance_430051_hype_v21_loop_20260523/binance_4300516091842181632_hype_universe_1m_20250524_20260524.npz`
- NPZ status: `ok`
- V21 tuning started: `True`
- PF audit: `/var/www/vps2.happyuser.info/top/top_1/obw_platform/meta_strategies/telegram_signal_dca/reports/binance_430051_hype_v21_loop_20260523/AUDIT_PF_20260524.md`
- IE=100 no-warmup/no-trend rerun: `/var/www/vps2.happyuser.info/top/top_1/obw_platform/meta_strategies/telegram_signal_dca/reports/binance_430051_hype_v21_loop_20260523/wave_002_initial_equity_100_no_warmup_no_trend/REPORT_IE100_NO_WARMUP_NO_TREND.md`
- Paper-live status: `/var/www/vps2.happyuser.info/top/top_1/obw_platform/meta_strategies/telegram_signal_dca/reports/binance_430051_hype_v21_loop_20260523/PAPER_LIVE_STATUS_20260524.md`
- Slippage telemetry/model report: `/var/www/vps2.happyuser.info/top/top_1/obw_platform/meta_strategies/telegram_signal_dca/reports/binance_430051_hype_v21_loop_20260523/SLIPPAGE_MODEL_REPORT_20260524.md`
- Loop structure report: `/var/www/vps2.happyuser.info/top/top_1/obw_platform/meta_strategies/telegram_signal_dca/reports/binance_430051_hype_v21_loop_20260523/LOOP_STRUCTURE_20260524.md`
- V21 vs plain report: `/var/www/vps2.happyuser.info/top/top_1/obw_platform/meta_strategies/telegram_signal_dca/reports/binance_430051_hype_v21_loop_20260523/V21_VS_PLAIN_20260524.md`

## Audit Notes

- `long_low_exposure` / `dca3` PF `36.6722` reconciles from raw trades: gross profit `161.8216685282133`, gross loss `-4.41264854219354`, `116` wins and `6` losses.
- Fees/slippage are included as `0.0005` fee per side and `0.0009380229915652661` slippage per side.
- Source closed-position history is also very high PF: lead closed PnL PF about `33.1552`, with `114` wins and `8` losses.
- No duplicate IDs, wrong side, or HYPE symbol mismatch found in `wave_002`.
- Caveat: historical PF is closed-position-only and excludes currently open MTM until Binance reports the position closed.
- Caveat: maxDD is computed against `initial_equity=10000` while per-signal target notional is `100`, so displayed maxDD is much smaller than a target-notional-normalized risk view.

## Initial Equity 100 Rerun

- Rerun status: `complete`
- Rerun tmux session: `binance_430051_hype_ie100_no_trend` completed and exited normally.
- The existing signal-side compare path was already no-warmup/no-trend for trade decisions; loop `warmup_days` only affects annual NPZ collection metadata, not entries.
- New explicit rerun used `initial_equity=100`, `target_notional=100`, direct Binance lead side, no contrarian close, no trend flip/suppress/resize, no warmup/trend gating.
- Best IE=100 result remains `long_low_exposure` / `dca3`: net PnL `157.4090199860198`, net `%` `157.4090199860198`, per-30d `%` `35.17797172092236`, PF `36.67223142311971`, maxDD `%` `-2.018916077864745`, win `%` `95.08196721311475`, positions `122`.
- Old IE=10000 same variant had the same net PnL/PF/win rate but net `%` `1.5740901998602386` and maxDD `%` `-0.020956093394892402`; the earlier tiny DD was equity-base normalization, not a PF arithmetic issue.

## Paper-Live / Slippage Telemetry

- Active paper-live direct follow-open daemon: `binance_veronicaUA_follow_open_paper`.
- Added sidecar telemetry daemon: `binance_veronicaUA_slippage_telemetry`.
- Current open paper position snapshot at `2026-05-24T04:50:46.249452Z`: `HYPEUSDT` `LONG`, entry exec `60.13135035000001`, current mark `60.01`, hypothetical exit exec `59.953710619999995`, notional `100.0`.
- Floating PnL after configured exit slippage: `-0.29541949243788723` USDT / `-0.29541949243788723%`.
- Closed paper PnL so far: `3.1091725770899803` USDT; combined closed + open mark-to-exit model: `2.813753084652093` USDT.
- Current paper-live daemon uses fixed slippage `9.38` bp per side and does not deduct exchange fees in `paper_pnl_usdt`.
- Telemetry JSONL now records signal entry/mark, entry exec, current mark, source, configured slippage, orderbook spread/depth proxy when available, unrealized PnL, and closed-trade execution metrics.

## Files

- Positions CSV: `/var/www/vps2.happyuser.info/top/top_1/obw_platform/meta_strategies/telegram_signal_dca/reports/binance_430051_hype_v21_loop_20260523/wave_002/position_refresh/position_history_normalized.csv`
- Universe file: `/var/www/vps2.happyuser.info/top/top_1/obw_platform/meta_strategies/telegram_signal_dca/reports/binance_430051_hype_v21_loop_20260523/universe_430051_hype.txt`
- Windows file: `/var/www/vps2.happyuser.info/top/top_1/obw_platform/meta_strategies/telegram_signal_dca/reports/binance_430051_hype_v21_loop_20260523/windows.json`

## Strategy Contract

- Entry is single-leg only.
- Entry side comes from this lead's Binance public position `side`.
- Trend does not flip, suppress, or resize entries.
- Consilium handles warmup/trend diagnostics and risk-gated candidate evaluation.

## Consilium Notes

- `.claude/agents/brain-evaluation.md`: Brain evaluates candidates and validates realism; MDD/risk penalties dominate small return gains.
- `.claude/agents/critic.md`: reject or flag unrealistic winners, overfit artifacts, impossible configs, excessive order frequency, high live risk, and weak execution assumptions.
- Treat Binance signal direction as authoritative for the entry leg.
- Do not flip, suppress, resize, or exit because the trend detector changed its opinion.
- Consilium may compute warmup and trend state as context and as a quality/risk diagnostic, but trend must not decide which leg to place.
- Treat parsed Telegram signal direction as authoritative for the entry leg.
- Do not react to trend after entry direction is known.
- Use consilium warmup/trend handling only to decide whether the data window is sufficiently initialized and to annotate market context.
- Tune V21 separately from Binance; do not mix performance journals or promote one source's champion into the other source without retesting.
- Reject candidates with min unrealized PnL below `-50%`; flag high risk if below `-40%`.

## Variants

| variant | best label | score | status |
|---|---|---:|---|
| long_low_exposure | dca3 | 0.351151 | ok |
| baseline | dca3 | 0.349456 | ok |
| long_aggressive_second_leg | dca3 | 0.347470 | ok |
| long_high_tp | dca3 | 0.347307 | ok |
| long_conservative_wide_grid | dca3 | 0.345288 | ok |
| long_balanced_hype_grid | dca3 | 0.344949 | ok |
