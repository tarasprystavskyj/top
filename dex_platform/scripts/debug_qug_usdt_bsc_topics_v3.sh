#!/usr/bin/env bash
set -euo pipefail

SCRIPT_VERSION="debug_qug_usdt_bsc_topics_v3_2026_05_02_full_file"
echo "[script_version] $0 SCRIPT_VERSION=${SCRIPT_VERSION}"

: "${BSC_RPC_URL:?BSC_RPC_URL is not set. Example: export BSC_RPC_URL='https://bsc-dataseed.binance.org/'}"

POOL="0xe1acb466421ed24dd8bd381d1205bad0ad43ca9c"
OUT_DIR="DEX_DATA/uniswap_v3_bsc/QUG_USDT_001_2026_05_01_v3"
mkdir -p "${OUT_DIR}"

python3 dex_platform/data_collectors/debug_evm_pool_logs_topics_v3.py \
  --rpc-env BSC_RPC_URL \
  --expected-chain-id 56 \
  --pool "${POOL}" \
  --time-from 2026-05-01T00:00:00Z \
  --time-to   2026-05-02T00:00:00Z \
  --out-csv "${OUT_DIR}/topic_counts.csv" \
  --chunk-size 2000 \
  --sleep-s 0.2
