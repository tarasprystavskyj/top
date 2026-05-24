# VeronicaUA / Binance 430051 Loop Structure

- Updated: `2026-05-24T05:08:26.151398Z`
- Guardrail: paper/backtest-only; no live orders.

## Active tmux Sessions

- `binance_430051_hype_v21_loop`: periodically refreshes public Binance lead history/open-position data, annual HYPE OHLCV/NPZ, and V21 compare/tune waves.
- `binance_veronicaUA_follow_open_paper`: paper-live listener following Binance public open-position direction directly, not contrarian close.
- `binance_veronicaUA_slippage_telemetry`: sidecar telemetry loop for mark/orderbook/PnL/slippage reports.

## Data Flow

1. Binance public open positions endpoint -> direct follow-open paper signal.
2. Paper-live daemon -> `veronicaUA_follow_open_state.json` and `veronicaUA_follow_open_paper.log`.
3. Sidecar telemetry -> `veronicaUA_slippage_telemetry.jsonl`, `PAPER_LIVE_STATUS.json`, `PAPER_LIVE_STATUS_20260524.md`, `SLIPPAGE_MODEL_REPORT_20260524.md`.
4. Binance public closed position history -> `wave_*/position_refresh/position_history_normalized.csv`.
5. HYPE OHLCV collection -> annual/window `.npz` under this report dir.
6. V21 compare/tune -> `wave_*/variants/*/summary.json` and IE=100 no-warmup/no-trend report.
7. Status/report aggregation -> `STATUS.md`, `STATUS.json`, audit and comparison reports.

## Agent Flow Used So Far

- Parent/Taras delegated Binance 430051/HYPE loop setup to this Codex worker.
- This worker owns only the Binance 430051/HYPE report/code paths here.
- Separate workers own Binance 475183 and DarkKnight loops; those were not modified for this task.
- Current addition is a sidecar telemetry/research loop, avoiding disruption of existing paper-live and tune loops.
