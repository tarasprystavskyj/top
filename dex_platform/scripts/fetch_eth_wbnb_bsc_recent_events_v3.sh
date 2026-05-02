#!/usr/bin/env bash
set -euo pipefail

SCRIPT_VERSION="fetch_eth_wbnb_bsc_recent_events_v3_2026_05_02_full_file"
echo "[script_version] $0 SCRIPT_VERSION=${SCRIPT_VERSION}"

: "${BSC_RPC_URL:?BSC_RPC_URL is not set. Example: export BSC_RPC_URL='https://bsc-dataseed.binance.org/'}"

POOL="0x62fcb3c1794fb95bd8b1a97f6ad5d8a7e4943a1e"
OUT_DIR="${1:-DEX_DATA/pancake_v3_bsc/ETH_WBNB_001_2026_05_01_v3}"

python3 dex_platform/data_collectors/check_evm_rpc_pool_v3.py \
  --rpc-env BSC_RPC_URL \
  --expected-chain-id 56 \
  --pool "${POOL}"

python3 dex_platform/data_collectors/fetch_cl_pool_events_evm_v3.py \
  --pool "${POOL}" \
  --rpc-env BSC_RPC_URL \
  --expected-chain-id 56 \
  --time-from 2026-05-01T00:00:00Z \
  --time-to   2026-05-02T00:00:00Z \
  --out-dir "${OUT_DIR}" \
  --chunk-size 2000 \
  --min-chunk-size 100 \
  --sleep-s 0.2 \
  --events Swap,Mint,Burn,Collect

python3 dex_platform/data_collectors/inspect_aerodrome_events.py \
  "${OUT_DIR}/events_all.csv"
