#!/usr/bin/env bash
set -euo pipefail

# Run from repo root, for example:
#   cd /var/www/vps2.happyuser.info/top/top_1
#   bash obw_platform/scripts/run_annual_dynamic_swarm_ena.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR/obw_platform"

NPZ_PATH="${NPZ_PATH:-../DB/ena_ohlcv_30s_1y_from_ticks.npz}"
BASE_CFG="${BASE_CFG:-configs/cfg_pack_dual_full_ena.yaml}"
SEED_CFG_1="${SEED_CFG_1:-search_seeds/best_month_seed.yaml}"
SEED_CFG_2="${SEED_CFG_2:-search_seeds/hybrid_avg_seed.yaml}"
REPORT_OUT="${REPORT_OUT:-../_reports/annual_dynamic_swarm_v1/report.json}"
BEST_CFG_OUT="${BEST_CFG_OUT:-../_reports/annual_dynamic_swarm_v1/champion.yaml}"

DYNAMIC_SLIPPAGE_JSON='{"kind":"linear_bp","base_bp":1.2,"coefficients":{"participation":220.0,"range_bp":0.04,"log_quote_volume":-0.06,"side_x_body_signed_bp":0.015,"is_exit":0.2},"clip_min_bp":0.25,"clip_max_bp":12.0}'

python autoresearch_annual_dynamic_swarm_v1.py \
  --cfg "$BASE_CFG" \
  --seed-cfg "$SEED_CFG_1" \
  --seed-cfg "$SEED_CFG_2" \
  --npz "$NPZ_PATH" \
  --window-days 30 \
  --n-windows 3 \
  --waves 2 \
  --variants-per-wave 4 \
  --topk-finalists 3 \
  --seed 42 \
  --dynamic-slippage-json "$DYNAMIC_SLIPPAGE_JSON" \
  --report-out "$REPORT_OUT" \
  --best-cfg-out "$BEST_CFG_OUT"
