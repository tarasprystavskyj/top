#!/usr/bin/env bash
set -euo pipefail
SCRIPT_VERSION="tune_check_2pct_fast_npz_v2_2026_05_02"
echo "[script_version] $0 SCRIPT_VERSION=${SCRIPT_VERSION}"

NPZ="${1:-DEX_DATA/fast_npz/base_CHECK_USDC_2PCT_2026_02_05_fee_replay_v2.npz}"
OUT_DIR="${2:-DEX_REPORTS/check_2pct_fast_npz_tune_v2}"

python3 dex_platform/backtest/cl_fee_replay_fast_npz_v2.py \
  --npz "$NPZ" \
  --out-dir "$OUT_DIR" \
  --initial-capital-usd 1000 \
  --fee-rates metadata_0_2515:0.002515 \
  --grid-lower 60,70,80,85,90,95 \
  --grid-upper 1,2,3,5,8,10 \
  --target-mdd-pct 25 \
  --max-liquidity-share-pct 5 \
  --max-avg-liquidity-share-pct 3 \
  --hard-max-liquidity-share
