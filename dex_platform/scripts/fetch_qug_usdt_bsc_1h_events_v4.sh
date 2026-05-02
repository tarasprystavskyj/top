#!/usr/bin/env bash
set -euo pipefail

SCRIPT_VERSION="fetch_qug_usdt_bsc_1h_events_v4_2026_05_02_full_file"
echo "[script_version] $0 SCRIPT_VERSION=${SCRIPT_VERSION}"

: "${BSC_RPC_URL:?BSC_RPC_URL is not set. Example: export BSC_RPC_URL='https://bsc-dataseed.binance.org/'}"

POOL="0xe1acb466421ed24dd8bd381d1205bad0ad43ca9c"
OUT_DIR="${1:-DEX_DATA/uniswap_v3_bsc/QUG_USDT_001_2026_05_01_1h_v4}"

python3 dex_platform/data_collectors/fetch_cl_pool_events_evm_v4.py \
  --pool "${POOL}" \
  --rpc-env BSC_RPC_URL \
  --expected-chain-id 56 \
  --time-from 2026-05-01T00:00:00Z \
  --time-to   2026-05-01T01:00:00Z \
  --out-dir "${OUT_DIR}" \
  --chunk-size 20 \
  --min-chunk-size 1 \
  --sleep-s 0.2 \
  --limit-retries 1 \
  --events Swap

python3 dex_platform/data_collectors/inspect_aerodrome_events.py \
  "${OUT_DIR}/events_all.csv"
