#!/usr/bin/env bash
set -euo pipefail

NPZ=${1:-DEX_DATA/fast_npz/base_CHECK_USDC_2PCT_2026_02_05_fee_replay_v2.npz}
OUT=${2:-DEX_REPORTS/check_2pct_capacity_walkforward_v3}

python3 dex_platform/backtest/cl_fee_replay_fast_npz_v3.py \
  --npz "$NPZ" \
  --out-dir "$OUT" \
  --fee-rates metadata_0_2515:0.002515 \
  --walkforward \
  --capital-mode grid \
  --capital-grid 25,50,75,100,125,150,175,200,250,300,500,1000 \
  --grid-lower 60,70,80,85,90,95 \
  --grid-upper 0.5,1,2,3,5,8,10 \
  --rebalance-grid none:0,periodic:168,periodic:336,oor:24 \
  --target-mdd-pct 25 \
  --max-avg-liquidity-share-pct 3 \
  --max-p95-liquidity-share-pct 5 \
  --max-p99-liquidity-share-pct 10 \
  --max-liquidity-share-pct 25 \
  --w-mdd 2 \
  --w-avg-share 5 \
  --w-p95-share 10 \
  --w-p99-share 3 \
  --w-max-share 0.5 \
  --w-rebalance 0.02
