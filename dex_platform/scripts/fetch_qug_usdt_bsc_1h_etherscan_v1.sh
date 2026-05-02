#!/usr/bin/env bash
set -euo pipefail

SCRIPT_VERSION="fetch_qug_usdt_bsc_1h_etherscan_v1_2026_05_02_full_file"
echo "[script_version] $0 SCRIPT_VERSION=${SCRIPT_VERSION}"

: "${ETHERSCAN_API_KEY:?ETHERSCAN_API_KEY is not set. Etherscan V2 key required.}"
: "${BSC_RPC_URL:?BSC_RPC_URL is not set. Needed only for time->block conversion.}"

POOL="0xe1acb466421ed24dd8bd381d1205bad0ad43ca9c"
OUT_DIR="${1:-DEX_DATA/uniswap_v3_bsc/QUG_USDT_001_2026_05_01_1h_etherscan_v1}"

python3 dex_platform/data_collectors/fetch_cl_pool_events_etherscan_v1.py \
  --chain-id 56 \
  --address "${POOL}" \
  --api-key-env ETHERSCAN_API_KEY \
  --rpc-env BSC_RPC_URL \
  --time-from 2026-05-01T00:00:00Z \
  --time-to   2026-05-01T01:00:00Z \
  --out-dir "${OUT_DIR}" \
  --events Swap \
  --block-chunk 20 \
  --min-block-chunk 1 \
  --max-logs-per-chunk 900 \
  --sleep-s 0.25

python3 dex_platform/data_collectors/inspect_aerodrome_events.py \
  "${OUT_DIR}/events_all.csv"
