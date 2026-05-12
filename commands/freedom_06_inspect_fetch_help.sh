#!/usr/bin/env bash
set -euo pipefail
ROOT="${PROJECT_ROOT:-$(pwd)}"
cd "$ROOT/obw_platform"
for f in fetch_build_cache_and_fast_v1.py fetch_build_cache_ohlcv_universe_v1.py fetch_build_futures_fast_cache_2m_v1.py; do
  echo "===== $f --help ====="
  python3 "$f" --help | sed -n '1,120p' || true
done
