#!/usr/bin/env bash
set -euo pipefail

# Run from top_1 root:
#   export BASE_RPC_URL="https://mainnet.base.org"
#   bash dex_platform/scripts/fetch_check_2pct_aerodrome_recent_events.sh

POOL="0x5a7b4970b2610aee4776a6944d9f2171ee6060b0"
OUT_DIR="DEX_DATA/aerodrome_slipstream/base_CHECK_USDC_2PCT_recent_2026_05_01"

python3 dex_platform/data_collectors/fetch_aerodrome_slipstream_events_v1.py \
  --pool "${POOL}" \
  --time-from 2026-05-01T00:00:00Z \
  --time-to   2026-05-02T00:00:00Z \
  --out-dir "${OUT_DIR}" \
  --chunk-size 3000 \
  --sleep-s 0.10

python3 dex_platform/data_collectors/inspect_aerodrome_events.py \
  "${OUT_DIR}/events_all.csv"
