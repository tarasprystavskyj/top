#!/usr/bin/env bash
set -euo pipefail

# Run from obw_platform root:
#   bash scripts/fetch_uniswap_v3_weth_usdc_005_smoke.sh

if [[ -z "${THEGRAPH_API_KEY:-}" ]]; then
  echo "ERROR: THEGRAPH_API_KEY is not set."
  echo "Run: export THEGRAPH_API_KEY='YOUR_KEY'"
  exit 1
fi

POOL="0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640"
FROM_TS="2026-04-01T00:00:00Z"
TO_TS="2026-04-02T00:00:00Z"
OUT_DIR="_data/uniswap_v3_weth_usdc_005_smoke_2026_04_01"

python3 dex_data_collectors/fetch_uniswap_v3_pool_events_v1.py \
  --pool "${POOL}" \
  --time-from "${FROM_TS}" \
  --time-to "${TO_TS}" \
  --out-dir "${OUT_DIR}" \
  --max-pages 2

python3 scripts/inspect_dex_events.py "${OUT_DIR}/events_all.csv"
