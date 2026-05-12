#!/usr/bin/env bash
set -euo pipefail

ROOT="/var/www/vps2.happyuser.info/top/top_1"
LANE="obw_platform/meta_strategies/akela_meta_short"
PROMPT="$ROOT/$LANE/MARGIN_ZERO_CODEX_PROMPT.md"
SLEEP_SECONDS="${OBW_MARGIN_ZERO_CODEX_SLEEP:-300}"
MAX_CYCLES="${OBW_MARGIN_ZERO_CODEX_MAX_CYCLES:-0}"
RUN_ROOT="$ROOT/_reports/akela_meta_short/margin_zero_codex_loop"

mkdir -p "$RUN_ROOT"
cd "$ROOT"

cycle=0
while true; do
  cycle=$((cycle + 1))
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  log="$RUN_ROOT/codex_${stamp}.log"

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
