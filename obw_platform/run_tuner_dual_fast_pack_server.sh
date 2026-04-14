#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFG="${CFG:-$ROOT_DIR/configs/cfg_pack_dual_full_ena.yaml}"
NPZ="${NPZ:-/mnt/data/fast_cache_30s_ENA_1y(1).npz}"
PLAN="${PLAN:-$ROOT_DIR/tuner_plans/pack_ena_quarter_v1.py}"
PREFIX="${PREFIX:-ena_1y_pack}"
JOBS="${JOBS:-$(python3 - <<'PY'
import os
print(max(1, (os.cpu_count() or 2) - 1))
PY
)}"
LIMIT_BARS="${LIMIT_BARS:-0}"
MIN_TRADES="${MIN_TRADES:-200}"
W_PNL="${W_PNL:-1.0}"
W_MDD="${W_MDD:-80.0}"
W_REALIZED_MDD="${W_REALIZED_MDD:-5.0}"

cd "$ROOT_DIR"
python3 auto_tuner_dual_fast_pack.py \
  --cfg "$CFG" \
  --npz "$NPZ" \
  --plan "$PLAN" \
  --prefix "$PREFIX" \
  --jobs "$JOBS" \
  --limit-bars "$LIMIT_BARS" \
  --min-trades "$MIN_TRADES" \
  --w-pnl "$W_PNL" \
  --w-mdd "$W_MDD" \
  --w-realized-mdd "$W_REALIZED_MDD" \
  "$@"
