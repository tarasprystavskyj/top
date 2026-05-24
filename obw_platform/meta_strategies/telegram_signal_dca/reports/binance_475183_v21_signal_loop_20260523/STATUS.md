# Binance 475183 V21 Signal Loop

Paper/backtest-only. No live orders. No `.env` secrets are read or printed.

- Updated: `2026-05-24T02:33:57.376714Z`
- tmux session: `binance_475183_v21_signal_loop`
- Lead: `4751838302089254401`
- Universe: `BTCUSDT, ETHUSDT`
- Window with warmup: `2025-12-23T15:34:06.214000Z` .. `2026-05-24T02:25:21.819836Z`
- Positions CSV: `/var/www/vps2.happyuser.info/top/top_1/obw_platform/meta_strategies/telegram_signal_dca/reports/binance_475183_v21_signal_loop_20260523/wave_002/position_refresh/position_history_normalized.csv`
- NPZ: `/var/www/vps2.happyuser.info/top/top_1/obw_platform/meta_strategies/telegram_signal_dca/reports/binance_475183_v21_signal_loop_20260523/binance_4751838302089254401_1m_20251223_20260524.npz`
- Latest wave: `2`

## Strategy Contract

- Entry is single-leg only.
- Entry side comes from Binance copy `contrarian_side` signal.
- The simulator does not flip, block, or resize entry by trend.
- Consilium philosophy is used for warmup/window discipline, bounded mutations, ranking, and promotion notes.

## Consilium Notes

- one wave at a time; maintain compact journal/status
- plan small bounded mutations plus at least one conservative candidate
- test candidates, rank by return with drawdown and margin-call penalties
- promote only if risk constraints remain acceptable
- human owns commits; loop writes reports and artifacts only
- source files: .claude/agents/orchestrator.md, .claude/agents/brain-planning.md, .claude/agents/brain-evaluation.md

## Variants

| variant | best label | score | status |
|---|---|---:|---|
| balanced_tighter_tp_grid | dca3 | 3.129357 | ok |
| aggressive_second_leg | dca3 | 2.927558 | ok |
| baseline | dca3 | 2.755572 | ok |
| short_defensive_wide_rise | dca2 | 2.744406 | ok |
| conservative_wide_grid | dca3 | 2.081402 | ok |
| long_defensive_wide_drop | dca3 | 1.682308 | ok |
