#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
NPZ="${1:-/mnt/data/ena_ohlcv_30s_1y_from_ticks(5).npz}"
python3 auto_tuner_dual_fast_pack.py \
  --cfg configs/final_best_ena_feeaware_logged_v1.yaml \
  --npz "$NPZ" \
  --plan tuner_plans/tuner_plan_ENA_smoke.py \
  --limit-bars 600 \
  --jobs 1 \
  --min-trades 1 \
  --w-pnl 1 \
  --w-mdd 120 \
  --w-realized-mdd 5 \
  --prefix smoke_feeaware_logged \
  --debug
