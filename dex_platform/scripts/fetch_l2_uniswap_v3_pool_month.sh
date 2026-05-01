#!/usr/bin/env bash
set -euo pipefail
NETWORK="${1:?network required: base|arbitrum|optimism}"
POOL="${2:?pool address required}"
LABEL="${3:-pool}"
FROM_TS="${4:-2026-03-01T00:00:00Z}"
TO_TS="${5:-2026-04-01T00:00:00Z}"
SAFE_FROM="$(echo "${FROM_TS}" | tr ':' '-' | tr -d 'Z')"
SAFE_TO="$(echo "${TO_TS}" | tr ':' '-' | tr -d 'Z')"
OUT_DIR="DEX_DATA/l2_uniswap_v3/${NETWORK}_${LABEL}_${SAFE_FROM}_${SAFE_TO}"

python3 dex_platform/data_collectors/fetch_uniswap_v3_l2_pool_events_v1.py \
  --network "${NETWORK}" \
  --pool "${POOL}" \
  --time-from "${FROM_TS}" \
  --time-to "${TO_TS}" \
  --out-dir "${OUT_DIR}"

python3 dex_platform/data_collectors/inspect_dex_events.py "${OUT_DIR}/events_all.csv"
