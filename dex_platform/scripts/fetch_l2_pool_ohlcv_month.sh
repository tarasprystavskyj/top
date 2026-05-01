#!/usr/bin/env bash
set -euo pipefail
NETWORK="${1:?network required}"
POOL="${2:?pool address required}"
LABEL="${3:-pool}"
FROM_TS="${4:-2026-03-01T00:00:00Z}"
TO_TS="${5:-2026-04-01T00:00:00Z}"
SAFE_FROM="$(echo "${FROM_TS}" | tr ':' '-' | tr -d 'Z')"
SAFE_TO="$(echo "${TO_TS}" | tr ':' '-' | tr -d 'Z')"
OUT_CSV="DEX_DATA/l2_ohlcv/${NETWORK}_${LABEL}_${SAFE_FROM}_${SAFE_TO}.csv"

python3 dex_platform/data_collectors/fetch_geckoterminal_pool_ohlcv_v1.py \
  --network "${NETWORK}" \
  --pool "${POOL}" \
  --timeframe hour \
  --aggregate 1 \
  --time-from "${FROM_TS}" \
  --time-to "${TO_TS}" \
  --out-csv "${OUT_CSV}"

echo "Wrote: ${OUT_CSV}"
