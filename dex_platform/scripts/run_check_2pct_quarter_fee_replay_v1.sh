#!/usr/bin/env bash
set -euo pipefail

# Run from top_1 root after quarter event dataset is collected:
#   bash dex_platform/scripts/run_check_2pct_quarter_fee_replay_v1.sh

EVENTS="${1:-DEX_DATA/aerodrome_slipstream/base_CHECK_USDC_2PCT_2026_02_05_v2/events_all.csv}"
OUT_DIR="${2:-DEX_REPORTS/aerodrome_check_2pct_quarter_fee_replay_v1}"

python3 dex_platform/backtest/aerodrome_slipstream_fee_replay_v1.py \
  --events "${EVENTS}" \
  --out-dir "${OUT_DIR}" \
  --initial-capital-usd 1000 \
  --fee-rates metadata_0_2685:0.002685,label_2pct:0.02
