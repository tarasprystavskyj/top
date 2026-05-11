#!/usr/bin/env bash
set -euo pipefail

ROOT="/var/www/vps2.happyuser.info/top/top_1"
LANE="obw_platform/meta_strategies/akela_meta_short"
SLEEP_SECONDS="${OBW_AKELA_LOOP_SLEEP:-1800}"
LOG_DIR="$ROOT/$LANE/logs"
mkdir -p "$LOG_DIR"

cd "$ROOT"

while true; do
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  log="$LOG_DIR/worker_${stamp}.log"

  {
    echo "[akela-worker] start ${stamp}"
    git branch --show-current 2>/dev/null || git rev-parse --abbrev-ref HEAD
    python3 "$LANE/akela_meta_iteration.py"
    echo "[akela-worker] iteration exit code $?"
  } >"$log" 2>&1 || true

  git add "$LANE"
  if ! git diff --cached --quiet -- "$LANE"; then
    git commit -m "akela meta worker: update ${stamp}" -- "$LANE" >>"$log" 2>&1 || true
  fi

  echo "[akela-worker] sleep ${SLEEP_SECONDS}s" >>"$log"
  sleep "$SLEEP_SECONDS"
done
