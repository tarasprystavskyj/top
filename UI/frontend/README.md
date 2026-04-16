## Backtest vs Live Validation page

- Route: `/backtest_live_validation`.
- Use **Source selection** to choose a CSV from `obw_platform/_reports/TV_backtest_source` or upload one manually.
- Click **Run / Refresh comparison** to trigger backend extraction (`extract_bingx_window_from_tv.py`) and matching.
- Polling cadence is inferred from the dominant timestamp interval in the selected TradingView CSV and exposed by the backend (`bar_interval_seconds`). The frontend applies a small delay (`interval + 2s`, floor 5s).
- Debugging: open the debug panel on the page to inspect payloads, polling state, backend counts, and stderr snippets.
