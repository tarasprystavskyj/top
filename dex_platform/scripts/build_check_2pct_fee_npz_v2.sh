#!/usr/bin/env bash
set -euo pipefail
SCRIPT_VERSION="build_check_2pct_fee_npz_v2_2026_05_02"
echo "[script_version] $0 SCRIPT_VERSION=${SCRIPT_VERSION}"

EVENTS="${1:-DEX_DATA/aerodrome_slipstream/base_CHECK_USDC_2PCT_2026_02_05_v2/events_all.parquet}"
OUT_NPZ="${2:-DEX_DATA/fast_npz/base_CHECK_USDC_2PCT_2026_02_05_fee_replay_v2.npz}"
mkdir -p "$(dirname "$OUT_NPZ")"

python3 dex_platform/data_collectors/build_cl_fee_replay_npz_v2.py \
  --events "$EVENTS" \
  --out-npz "$OUT_NPZ" \
  --pool-name base_CHECK_USDC_AERODROME_2PCT \
  --token0 USDC \
  --token1 CHECK \
  --dec0 6 \
  --dec1 18 \
  --quote-token token0 \
  --fee-rate 0.002515
