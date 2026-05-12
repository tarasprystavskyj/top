# Data Collection Plan for FREEDOMMONEY

## Goal

Get more than the current uploaded 30d/short FREEDOMMONEY sample.

Target windows:

- 30d
- 90d
- 180d
- if exchange supports it: 365d

Timeframes:

- primary: 1m
- secondary: 3m if cost/range improves
- avoid 30s unless data proves range/cost ratio is strong

## Existing scripts to inspect

In `obw_platform`:

```text
fetch_build_cache_and_fast_v1.py
fetch_build_cache_ohlcv_universe_v1.py
fetch_build_futures_fast_cache_2m_v1.py
akela_research_cache_builder_v2.py
rank_short_leg_all_symbols_akela_v2.py
monthly_akela_phase_proxybt.py
rank_fast_cache_akela_phase_proxybt.py
```

## Discovery commands

```bash
cd "$OBW_DIR"
python3 obw_platform/tools/freedom_find_data_v1.py --root "$PROJECT_ROOT" --symbol FREEDOMMONEY
```

If no local data exists, inspect fetch script help:

```bash
cd "$OBW_DIR"
python3 fetch_build_cache_and_fast_v1.py --help
python3 fetch_build_cache_ohlcv_universe_v1.py --help
```

Then build a single-symbol universe:

```bash
mkdir -p universe DB _reports/freedommoney
printf 'FREEDOMMONEY/USDT:USDT\n' > universe/universe_FREEDOMMONEY_1m.txt
```

Use the script whose CLI matches the installed repo version. Do not guess silently. Run `--help`, then adapt.
