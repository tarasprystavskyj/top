# Binance Copy Lead 4728671486012660992

Research/paper-only preparation for:

`https://www.binance.com/uk-UA/copy-trading/lead-details/4728671486012660992?timeRange=365D&isSmartFilter=true`

## Status

- The lead ID is already included in `collect_data_then_tune.py` and `run_night_rough_tune.py`.
- `paper_live_binance_copy_public_positions.py` defaults to this lead and polls only public Binance copy-trading endpoints.
- A one-shot paper poll on 2026-05-25 saw 4 open public positions and 20 history rows.
- No live orders, account secrets, or private account pages are used.

## Commands

Single poll that writes local paper state:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_binance_472867_public_paper_once.ps1
```

Paper loop, still read-only and order-free:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_binance_472867_public_paper_loop.ps1 -NotionalUsdt 100 -IntervalSec 60
```

State path:

`obw_platform/meta_strategies/telegram_signal_dca/reports/binance_copy_4728671486012660992_paper_20260525/paper_live_state.json`

## Gates

- Do not promote to live trading without explicit approval.
- Treat Binance `avgCost` / `avgClosePrice` historical replay as artifact baseline.
- Before any promotion, calibrate executable BingX entry/exit after public signal observation and measure slippage/latency.
- Keep emergency risk controls separate from alpha logic.
