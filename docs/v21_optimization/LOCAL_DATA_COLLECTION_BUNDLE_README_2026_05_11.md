# Local Data Collection Bundle - 2026-05-11

This bundle tests whether the server problem is IP/network related and, if data collection works locally, builds one-year NPZ files and V21 live-candidate configs for:

- `FREEDOMMONEY`
- `MAXXING`
- `CHECK`

## Why This Bundle Exists

On the Codex/server execution sandbox, all public ccxt endpoints failed before authentication:

- BingX: `open-api.bingx.com`
- Bybit: `api.bybit.com`
- Gate.io: `api.gateio.ws`

The failure happened at DNS/connection level, so authenticated API keys are not expected to fix it. Running this bundle locally will show whether the issue is the server/sandbox network path or a broader exchange-side block.

## Files

- `obw_platform/scripts/probe_exchange_connectivity_20260511.py`
- `obw_platform/scripts/fetch_backfill_ohlcv_npz_from_now_v1.py`
- `obw_platform/scripts/run_v21_live_candidate_data_and_tuners_multi_exchange_20260511.sh`
- `obw_platform/tuner_plans/tuner_plan_V21_live_candidates_1m_1y.py`
- `obw_platform/universe/universe_v21_live_candidates_freedommoney_maxxing_check.txt`

## Quick Test

From the project root:

```bash
python3 obw_platform/scripts/probe_exchange_connectivity_20260511.py
```

Expected good result: at least one exchange has `"ok": true`.

## Full Run

From the project root:

```bash
chmod +x obw_platform/scripts/run_v21_live_candidate_data_and_tuners_multi_exchange_20260511.sh
EXCHANGES="bingx bybit gateio" \
SYMBOLS="FREEDOMMONEY MAXXING CHECK" \
JOBS=1 \
./obw_platform/scripts/run_v21_live_candidate_data_and_tuners_multi_exchange_20260511.sh
```

Outputs:

- NPZ files: `DB/fast_cache_1m_<symbol>_1y_<exchange>.npz`
- tuner logs: `_reports/v21_live_candidates_1m_1y_20260511/`
- final configs: `obw_platform/configs/V21_<symbol>_<exchange>_live_candidate_1m_1y.yaml`

## Notes

- The default is `1m` OHLCV for `525600` bars. This is the practical exchange API path.
- The runner uses backward OHLCV backfill from the newest bars. For newly listed symbols, it collects the maximum available history instead of failing when the requested one-year start date is older than the listing.
- V21 was originally validated on ENA 30s data, so configs produced from 1m data should be treated as candidates and validated before live capital.
- If BingX fails but Bybit/Gate.io works, compare symbol availability and market symbol resolution. The script tries symbols as USDT perpetuals through ccxt.
