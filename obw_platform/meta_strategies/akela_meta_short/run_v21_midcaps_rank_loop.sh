#!/usr/bin/env bash
set -euo pipefail

ROOT="/var/www/vps2.happyuser.info/top/top_1"
UNIVERSE="$ROOT/_reports/akela_meta_short/bingx_marketcap_universe/universe_bingx_between_ena_xrp.txt"
OUT_DIR="$ROOT/_reports/akela_meta_short/v21_midcaps_rank"
PASSIVE_DB="$ROOT/_reports/akela_meta_short/s0_passive_orderbook_midcaps/s0_passive_orderbook.sqlite"
LOG_DIR="$OUT_DIR/logs"
mkdir -p "$LOG_DIR"
cd "$ROOT"

if [[ ! -f "$UNIVERSE" ]]; then
  python3 obw_platform/meta_strategies/akela_meta_short/build_bingx_marketcap_universe.py
fi
SYMBOLS="$(paste -sd, "$UNIVERSE")"

while true; do
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  log="$LOG_DIR/rank_midcaps_${stamp}.log"
  {
    echo "[rank-midcaps-loop] start ${stamp}"
    python3 obw_platform/meta_strategies/akela_meta_short/rank_v21_majors_liquidity_volatility.py \
      --symbols "$SYMBOLS" \
      --out-dir "$OUT_DIR" \
      --passive-db "$PASSIVE_DB" \
      --bars "${V21_MIDCAPS_RANK_BARS:-1500}" \
      --limit-bars "${V21_MIDCAPS_RANK_LIMIT_BARS:-1500}" \
      --timeframe "${V21_MIDCAPS_RANK_TIMEFRAME:-5m}"
    echo "[rank-midcaps-loop] exit=$?"
  } >"$log" 2>&1 || true
  find "$LOG_DIR" -maxdepth 1 -type f -name 'rank_midcaps_*.log' -printf '%T@ %p\n' \
    | sort -nr | awk 'NR > 24 {print $2}' | xargs -r rm -f
  sleep "${V21_MIDCAPS_RANK_SLEEP_SEC:-3600}"
done
