#!/usr/bin/env bash
set -euo pipefail

ROOT="/var/www/vps2.happyuser.info/top/top_1"
LOG_DIR="$ROOT/_reports/akela_meta_short/freedommoney_live_prep/watchdog_logs"
mkdir -p "$LOG_DIR"
cd "$ROOT"

python3 obw_platform/meta_strategies/akela_meta_short/live_freedommoney/freedommoney_telemetry_watchdog.py \
  --interval-sec "${FREEDOMMONEY_WATCHDOG_INTERVAL_SEC:-5}" \
  2>&1 | tee -a "$LOG_DIR/watchdog_$(date -u +%Y%m%dT%H%M%SZ).log"
