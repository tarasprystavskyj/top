#!/usr/bin/env bash
set -euo pipefail

ROOT="/var/www/vps2.happyuser.info/top/top_1"
OUT_DIR="$ROOT/_reports/akela_meta_short/s0_passive_orderbook"
LOG_DIR="$OUT_DIR/logs"
mkdir -p "$LOG_DIR"
cd "$ROOT"

python3 obw_platform/meta_strategies/akela_meta_short/s0_passive_telemetry_collector.py \
  --interval-sec "${S0_PASSIVE_INTERVAL_SEC:-10}" \
  --summary-every-sec "${S0_PASSIVE_SUMMARY_EVERY_SEC:-300}" \
  2>&1 | tee -a "$LOG_DIR/passive_$(date -u +%Y%m%dT%H%M%SZ).log"
