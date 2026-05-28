#!/usr/bin/env bash
set -euo pipefail

ROOT="/var/www/vps2.happyuser.info/top/top_1"
OUT_DIR="$ROOT/_reports/akela_meta_short/v21_majors_rank"
LOG_DIR="$OUT_DIR/logs"
mkdir -p "$LOG_DIR"
cd "$ROOT"

while true; do
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  log="$LOG_DIR/rank_${stamp}.log"
  {
    echo "[rank-loop] start ${stamp}"
    python3 obw_platform/meta_strategies/akela_meta_short/rank_v21_majors_liquidity_volatility.py \
      --bars "${V21_MAJORS_RANK_BARS:-5000}" \
      --limit-bars "${V21_MAJORS_RANK_LIMIT_BARS:-5000}" \
      --timeframe "${V21_MAJORS_RANK_TIMEFRAME:-5m}"
    echo "[rank-loop] exit=$?"
  } >"$log" 2>&1 || true
  find "$LOG_DIR" -maxdepth 1 -type f -name 'rank_*.log' -printf '%T@ %p\n' \
    | sort -nr | awk 'NR > 48 {print $2}' | xargs -r rm -f
  sleep "${V21_MAJORS_RANK_SLEEP_SEC:-1800}"
done
