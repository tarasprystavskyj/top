#!/usr/bin/env bash
set -euo pipefail

SCRIPT_VERSION="verify_bsc_qug_pool_v3_2026_05_02_full_file"
echo "[script_version] $0 SCRIPT_VERSION=${SCRIPT_VERSION}"

: "${BSC_RPC_URL:?BSC_RPC_URL is not set. Example: export BSC_RPC_URL='https://bsc-dataseed.binance.org/'}"

POOL="0xe1acb466421ed24dd8bd381d1205bad0ad43ca9c"

python3 dex_platform/data_collectors/check_evm_rpc_pool_v3.py \
  --rpc-env BSC_RPC_URL \
  --expected-chain-id 56 \
  --pool "${POOL}"
