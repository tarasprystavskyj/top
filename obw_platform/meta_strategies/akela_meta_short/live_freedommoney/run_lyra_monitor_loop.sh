#!/usr/bin/env bash
set -euo pipefail

ROOT="/var/www/vps2.happyuser.info/top/top_1"
SLEEP_SECONDS="${LYRA_FREEDOMMONEY_SLEEP:-1800}"
LOG_DIR="$ROOT/_reports/akela_meta_short/freedommoney_live_prep/lyra_logs"
mkdir -p "$LOG_DIR"
cd "$ROOT"

while true; do
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  log="$LOG_DIR/lyra_${stamp}.log"
  {
    echo "[lyra] start ${stamp}"
    python3 obw_platform/meta_strategies/akela_meta_short/live_freedommoney/lyra_live_monitor.py
    echo "[lyra] exit code $?"
  } >"$log" 2>&1 || true
  find "$LOG_DIR" -maxdepth 1 -type f -name 'lyra_*.log' -printf '%T@ %p\n' \
    | sort -nr | awk 'NR > 96 {print $2}' | xargs -r rm -f
  sleep "$SLEEP_SECONDS"
done
