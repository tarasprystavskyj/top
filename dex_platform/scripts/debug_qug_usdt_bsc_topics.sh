#!/usr/bin/env bash
set -euo pipefail

# Debug raw topics for QUG/USDT BSC pool if normal collector returns 0 rows.
# Run from top_1 root:
#   export BSC_RPC_URL="https://bsc-dataseed.binance.org/"
#   bash dex_platform/scripts/debug_qug_usdt_bsc_topics.sh

: "${BSC_RPC_URL:?BSC_RPC_URL is not set.}"

POOL="0xe1acb466421ed24dd8bd381d1205bad0ad43ca9c"
OUT_DIR="DEX_DATA/uniswap_v3_bsc/QUG_USDT_001_2026_05_01_v2"
mkdir -p "${OUT_DIR}"

python3 dex_platform/data_collectors/debug_evm_pool_logs_topics_v1.py \
  --rpc-env BSC_RPC_URL \
  --pool "${POOL}" \
  --time-from 2026-05-01T00:00:00Z \
  --time-to   2026-05-02T00:00:00Z \
  --out-csv "${OUT_DIR}/topic_counts.csv" \
  --chunk-size 2000 \
  --sleep-s 0.2
