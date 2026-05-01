#!/usr/bin/env bash
set -euo pipefail

# Run from top_1 root:
#   bash dex_platform/scripts/run_lp_proxy_mar2026.sh

EVENTS="${1:-DEX_DATA/uniswap_v3_weth_usdc_030_2026_03/events_all.csv}"
OUT_DIR="${2:-DEX_REPORTS/lp_proxy_weth_usdc_030_2026_03}"

python3 dex_platform/backtest/lp_event_proxy_backtester_v1.py \
  --events "${EVENTS}" \
  --out-dir "${OUT_DIR}" \
  --strategy both \
  --initial-capital-usd 10000 \
  --fee-tier-bps 30 \
  --active-liquidity-usd 10000000 \
  --wide-lower-pct 25 \
  --wide-upper-pct 18 \
  --inner-width-pct 1.0 \
  --outer-offset-pct 0.5 \
  --outer-width-pct 1.5 \
  --rebalance-hours 12 \
  --gas-per-rebalance-usd 25 \
  --rebalance-swap-cost-bps 5
