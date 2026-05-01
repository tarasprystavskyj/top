#!/usr/bin/env bash
set -euo pipefail

# Run from top_1 root:
#   export BASE_RPC_URL="https://base-mainnet.g.alchemy.com/v2/YOUR_KEY"
#   bash dex_platform/scripts/fetch_check_2pct_aerodrome_quarter_events_v2.sh

POOL="0x5a7b4970b2610aee4776a6944d9f2171ee6060b0"
OUT_DIR="${1:-DEX_DATA/aerodrome_slipstream/base_CHECK_USDC_2PCT_2026_02_05_v2}"

python3 dex_platform/data_collectors/fetch_aerodrome_slipstream_events_v2.py \
  --pool "${POOL}" \
  --time-from 2026-02-01T00:00:00Z \
  --time-to   2026-05-01T00:00:00Z \
  --out-dir "${OUT_DIR}" \
  --chunk-size 3000 \
  --min-chunk-size 100 \
  --sleep-s 0.15 \
  --events Swap,Mint,Burn,Collect

python3 dex_platform/data_collectors/inspect_aerodrome_events.py \
  "${OUT_DIR}/events_all.csv"
