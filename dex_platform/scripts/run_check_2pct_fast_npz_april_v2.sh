#!/usr/bin/env bash
set -euo pipefail
SCRIPT_VERSION="run_check_2pct_fast_npz_april_v2_2026_05_02"
echo "[script_version] $0 SCRIPT_VERSION=${SCRIPT_VERSION}"

NPZ="${1:-DEX_DATA/fast_npz/base_CHECK_USDC_2PCT_2026_02_05_fee_replay_v2.npz}"
OUT_DIR="${2:-DEX_REPORTS/check_2pct_fast_npz_april_v2}"

python3 dex_platform/backtest/cl_fee_replay_fast_npz_v2.py \
  --npz "$NPZ" \
  --out-dir "$OUT_DIR" \
  --initial-capital-usd 1000 \
  --fee-rates metadata_0_2515:0.002515 \
  --time-from 2026-04-01T00:00:00Z \
  --time-to   2026-05-01T00:00:00Z \
  --strategies wide_95_2:95:2,wide_90_2:90:2,wide_90_5:90:5,wide_80_5:80:5,wide_70_5:70:5,oor_90_2_24h:90:2:24:0.05:5:oor,oor_80_5_24h:80:5:24:0.05:5:oor,periodic_90_2_168h:90:2:168:0.05:5:periodic \
  --target-mdd-pct 25 \
  --max-liquidity-share-pct 5 \
  --max-avg-liquidity-share-pct 3 \
  --hard-max-liquidity-share \
  --plots
