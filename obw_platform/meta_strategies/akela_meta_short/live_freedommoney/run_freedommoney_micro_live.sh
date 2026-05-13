#!/usr/bin/env bash
set -euo pipefail

ROOT="/var/www/vps2.happyuser.info/top/top_1"
CFG="$ROOT/obw_platform/meta_strategies/akela_meta_short/live_freedommoney/V21_freedommoney_bingx_live_min2p2.yaml"
UNIVERSE="$ROOT/obw_platform/universe/universe_freedommoney_live.txt"
LIVE_DIR="$ROOT/obw_platform/_reports/_live/bingx_freedommoney_v21_min2p2"
mkdir -p "$LIVE_DIR"
cd "$ROOT/obw_platform"

python3 bt_live_paper_runner_separated_universe_4.py \
  --mode live \
  --cfg "$CFG" \
  --exchange bingx \
  --symbol-format usdtm \
  --results-dir "$LIVE_DIR" \
  --session-db session.sqlite \
  --cache-out combined_cache_session.db \
  --universe-file "$UNIVERSE" \
  --allow-symbols "FREEDOMMONEY/USDT:USDT" \
  --poll-sec "${FREEDOMMONEY_LIVE_POLL_SEC:-2}" \
  --bar-delay-sec "${FREEDOMMONEY_LIVE_BAR_DELAY_SEC:-1}" \
  --limit_klines "${FREEDOMMONEY_LIVE_LIMIT_KLINES:-360}" \
  --debug
