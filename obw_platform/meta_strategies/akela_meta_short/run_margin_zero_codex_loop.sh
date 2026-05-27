#!/usr/bin/env bash
set -euo pipefail

ROOT="/var/www/vps2.happyuser.info/top/top_1"
LANE="obw_platform/meta_strategies/akela_meta_short"
PROMPT="$ROOT/$LANE/MARGIN_ZERO_CODEX_PROMPT.md"
SLEEP_SECONDS="${OBW_MARGIN_ZERO_CODEX_SLEEP:-300}"
MAX_CYCLES="${OBW_MARGIN_ZERO_CODEX_MAX_CYCLES:-0}"
RUN_ROOT="$ROOT/_reports/akela_meta_short/margin_zero_codex_loop"
MIN_FREE_MB="${OBW_MARGIN_ZERO_CODEX_MIN_FREE_MB:-1024}"
LOG_KEEP="${OBW_MARGIN_ZERO_CODEX_LOG_KEEP:-30}"

mkdir -p "$RUN_ROOT"
cd "$ROOT"

prune_old_logs() {
  find "$RUN_ROOT" -maxdepth 1 -type f -name 'codex_*.log' -printf '%T@ %p\n' \
    | sort -nr \
    | awk -v keep="$LOG_KEEP" 'NR > keep {print $2}' \
    | xargs -r rm -f
}

free_mb() {
  df -Pm "$ROOT" | awk 'NR==2 {print $4}'
}

cycle=0
while true; do
  cycle=$((cycle + 1))
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  log="$RUN_ROOT/codex_${stamp}.log"
  prune_old_logs
  available_mb="$(free_mb)"
  if [[ "$available_mb" -lt "$MIN_FREE_MB" ]]; then
    {
      echo "[margin-zero-codex] start ${stamp}"
      echo "[margin-zero-codex] skipped: free disk ${available_mb}MB below ${MIN_FREE_MB}MB"
      echo "[margin-zero-codex] clean ignored _reports artifacts or lower OBW_MARGIN_ZERO_CODEX_MIN_FREE_MB"
    } >"$log" 2>&1
    echo "[margin-zero-codex] disk guard skipped cycle, log ${log}"
    if [[ "$MAX_CYCLES" != "0" && "$cycle" -ge "$MAX_CYCLES" ]]; then
      echo "[margin-zero-codex] reached max cycles ${MAX_CYCLES}"
      exit 0
    fi
    sleep "$SLEEP_SECONDS"
    continue
  fi

  {
    echo "[margin-zero-codex] start ${stamp}"
    echo "[margin-zero-codex] cycle ${cycle}"
    echo "[margin-zero-codex] branch $(git branch --show-current 2>/dev/null || git rev-parse --abbrev-ref HEAD)"
    echo "[margin-zero-codex] prompt ${PROMPT}"
    codex exec \
      --cd "$ROOT" \
      --sandbox danger-full-access \
      -c model_reasoning_effort=\"medium\" \
      - <"$PROMPT"
    echo "[margin-zero-codex] codex exit code $?"
  } >"$log" 2>&1 || true

  echo "[margin-zero-codex] log ${log}"

  if [[ "$MAX_CYCLES" != "0" && "$cycle" -ge "$MAX_CYCLES" ]]; then
    echo "[margin-zero-codex] reached max cycles ${MAX_CYCLES}"
    exit 0
  fi

  echo "[margin-zero-codex] sleep ${SLEEP_SECONDS}s" >>"$log"
  sleep "$SLEEP_SECONDS"
done
