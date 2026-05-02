#!/usr/bin/env bash
set -euo pipefail

# Verify BSC RPC and QUG/USDT pool existence.
# Run from top_1 root:
#   export BSC_RPC_URL="https://bsc-dataseed.binance.org/"
#   bash dex_platform/scripts/verify_bsc_qug_pool.sh

: "${BSC_RPC_URL:?BSC_RPC_URL is not set. Example: export BSC_RPC_URL='https://bsc-dataseed.binance.org/'}"

POOL="0xe1acb466421ed24dd8bd381d1205bad0ad43ca9c"

python3 dex_platform/data_collectors/check_evm_rpc_pool_v1.py \
  --rpc-env BSC_RPC_URL \
  --expected-chain-id 56 \
  --pool "${POOL}"
