# ENA Second-Leg 30s Data Runbook

Goal: build a second-leg one-year, 30-second OHLCV NPZ compatible with
`DB/ena_ohlcv_30s_1y_from_ticks_compat_np1.npz`.

## Existing Compatible Scripts

- `obw_platform/fetch_build_cache_and_fast_v1.py`
  - Unified builder for SQLite `price_indicators` and fast NPZ.
  - Supports `--source trades_api` for sub-minute bars and `--source local_ticks` for local monthly tick JSONL files.
- `obw_platform/fetch_build_cache_from_ticks_v1.py`
  - Older trade/tick API to SQLite builder for arbitrary bars including `30s`.
- `obw_platform/build_fast_ohlcv_npz_from_db_v2.py`
  - Exports the ENA-compatible OHLCV NPZ schema from a SQLite `price_indicators` DB.
- `obw_platform/build_fast_cache_from_local_ticks_v1.py`
  - Wrapper for local monthly tick JSONL files.

## Candidate Universe

`obw_platform/universe/universe_ena_second_leg_candidates.txt` contains:

```text
BTC
ETH
SOL
BNB
XRP
```

Preferred first pass is BTC/USDT:USDT and ETH/USDT:USDT because they are liquid perpetuals. The ranking script can decide between available NPZs after data exists.

## Data Collection Commands

These commands are documented but were not run here because a full one-year 30s trade pull is a network-heavy data collection job.

Build a multi-symbol candidate NPZ directly from public trade API:

```powershell
python obw_platform/fetch_build_cache_and_fast_v1.py `
  -i obw_platform/universe/universe_ena_second_leg_candidates.txt `
  -t 30s `
  --start "2025-03-01 00:00:00" `
  --end "2026-03-02 00:00:00" `
  --exchange bybit `
  --ccxt-symbol-format usdtm `
  --source trades_api `
  --db-out DB/combined_cache_30s_ena_second_leg_candidates_1y.db `
  --npz-out DB/ohlcv_30s_ena_second_leg_candidates_1y.npz `
  --feature-set full `
  --fresh `
  --debug
```

If local monthly tick JSONL files already exist for one candidate, use local ticks instead:

```powershell
python obw_platform/fetch_build_cache_and_fast_v1.py `
  --source local_ticks `
  --ticks-dir DB/BTCUSDT-bybit-2025-03-01-2026-03-01-YYYYMMDD_HHMMSS `
  --timeframe 30s `
  --start "2025-03-01 00:00:00" `
  --end "2026-03-02 00:00:00" `
  --market-symbol BTC/USDT:USDT `
  --db-out DB/combined_cache_30s_BTC_1y_from_ticks.db `
  --npz-out DB/btc_ohlcv_30s_1y_from_ticks.npz `
  --feature-set full `
  --fresh `
  --debug
```

If a SQLite DB has already been collected and only the ENA-compatible NPZ is needed:

```powershell
python obw_platform/build_fast_ohlcv_npz_from_db_v2.py `
  --db DB/combined_cache_30s_BTC_1y_from_ticks.db `
  --symbol BTC/USDT:USDT `
  --out DB/btc_ohlcv_30s_1y_from_ticks_compat_np1.npz `
  --ohlc-mode native `
  --meta-out DB/btc_ohlcv_30s_1y_from_ticks.meta.json
```

## Offline Ranking

After candidate NPZs exist, rank them against ENA:

```powershell
python obw_platform/tools/rank_ena_second_leg_npz.py `
  --ena-npz DB/ena_ohlcv_30s_1y_from_ticks_compat_np1.npz `
  --candidate-glob "DB/*30s*1y*.npz" `
  --out-dir docs/ena_second_leg_data/reports
```

The ranker is read-only for inputs and writes:

- `docs/ena_second_leg_data/reports/ena_second_leg_rank.csv`
- `docs/ena_second_leg_data/reports/ena_second_leg_rank.json`
- `docs/ena_second_leg_data/reports/ena_second_leg_rank.md`
- `docs/ena_second_leg_data/reports/ena_second_leg_rank_manifest.json`

Metrics: timestamp overlap, 30s gap ratio, return correlation, log-price hedge ratio, rolling hedge beta stability, ADF-style spread stationarity proxy, half-life proxy, zero-crossings per day, and quote-volume sanity.

## Collect, Rank, and Notify Main Codex Session

`docs/ena_second_leg_data/run_ena_second_leg_collect_rank_notify.ps1` wraps the documented collection command, then runs the offline ranker, then writes a Codex resume prompt to:

`docs/ena_second_leg_data/reports/codex_resume_prompt.md`

Dry-run the full command plan:

```powershell
powershell -ExecutionPolicy Bypass -File docs/ena_second_leg_data/run_ena_second_leg_collect_rank_notify.ps1 -DryRun
```

Rank existing candidate NPZs without running collection, then print the resume command:

```powershell
powershell -ExecutionPolicy Bypass -File docs/ena_second_leg_data/run_ena_second_leg_collect_rank_notify.ps1 -SkipCollect
```

Rank existing candidate NPZs and invoke `codex resume`:

```powershell
powershell -ExecutionPolicy Bypass -File docs/ena_second_leg_data/run_ena_second_leg_collect_rank_notify.ps1 -SkipCollect -InvokeCodexResume
```

Run collection too only after explicitly accepting the network-heavy trade download:

```powershell
powershell -ExecutionPolicy Bypass -File docs/ena_second_leg_data/run_ena_second_leg_collect_rank_notify.ps1 -InvokeCodexResume
```

The default resume target is `019e278d-e0a2-7430-8dde-fdc029a7802f`, so the invoked command is:

```powershell
codex resume 019e278d-e0a2-7430-8dde-fdc029a7802f <result prompt>
```

Pass `-CodexResumeId <SESSION_ID>` to override it. The wrapper does not read secrets and does not start live trading services. It calls public data collection only when `-SkipCollect` is omitted. If your Codex CLI expects a different `resume` prompt syntax, run the printed command manually and provide the contents of `docs/ena_second_leg_data/reports/codex_resume_prompt.md`.

## Current Data Availability

At this branch point, `DB` has ENA 30s NPZs but no BTC/ETH/SOL/BNB/XRP one-year 30s NPZ. No candidate recommendation is possible until at least one second-leg candidate NPZ is collected or provided.
