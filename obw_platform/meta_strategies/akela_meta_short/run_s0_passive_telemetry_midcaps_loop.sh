#!/usr/bin/env bash
set -euo pipefail

ROOT="/var/www/vps2.happyuser.info/top/top_1"
UNIVERSE="$ROOT/_reports/akela_meta_short/bingx_marketcap_universe/universe_bingx_between_ena_xrp.txt"
OUT_DIR="$ROOT/_reports/akela_meta_short/s0_passive_orderbook_midcaps"
LOG_DIR="$OUT_DIR/logs"
mkdir -p "$LOG_DIR"
cd "$ROOT"

if [[ ! -f "$UNIVERSE" ]]; then
  python3 obw_platform/meta_strategies/akela_meta_short/build_bingx_marketcap_universe.py
fi
SYMBOLS="$(paste -sd, "$UNIVERSE")"

python3 obw_platform/meta_strategies/akela_meta_short/s0_passive_telemetry_collector.py \
  --symbols "$SYMBOLS" \
  --out-dir "$OUT_DIR" \
  --interval-sec "${S0_PASSIVE_MIDCAP_INTERVAL_SEC:-20}" \
  --summary-every-sec "${S0_PASSIVE_MIDCAP_SUMMARY_EVERY_SEC:-300}" \
  2>&1 | tee -a "$LOG_DIR/passive_midcaps_$(date -u +%Y%m%dT%H%M%SZ).log"
