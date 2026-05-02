#!/usr/bin/env bash
set -euo pipefail

cd /var/www/vps2.happyuser.info/top/top_1

: "${BSC_RPC_URL:?BSC_RPC_URL is not set. Example: export BSC_RPC_URL='https://bsc-dataseed.binance.org/'}"

python3 dex_platform/patches/test_bsc_poa_web3_v1.py

grep -R "_inject_poa_middleware" -n \
  dex_platform/data_collectors/fetch_aerodrome_slipstream_events_v2.py \
  dex_platform/data_collectors/check_evm_rpc_pool_v1.py \
  dex_platform/data_collectors/debug_evm_pool_logs_topics_v1.py

bash dex_platform/scripts/verify_bsc_qug_pool.sh
