#!/usr/bin/env bash
set -euo pipefail
ROOT="${PROJECT_ROOT:-$(pwd)}"
ENV_MAIN="$ROOT/.agent/freedom_relay.env"
ENV_MODEL="$ROOT/.agent/freedom_model.env"
if [[ -f "$ENV_MAIN" ]]; then set -a; source "$ENV_MAIN"; set +a; fi
if [[ -f "$ENV_MODEL" ]]; then set -a; source "$ENV_MODEL"; set +a; fi
cd "$ROOT"
mkdir -p _reports/freedommoney/claude_loop docs/freedommoney_handoff
ITER_FILE="docs/freedommoney_handoff/LOOP_ITERATION.txt"
FAIL_FILE="docs/freedommoney_handoff/LAST_LOOP_FAILED.flag"
ITER=0
[[ -f "$ITER_FILE" ]] && ITER=$(cat "$ITER_FILE" 2>/dev/null || echo 0)
while true; do
  ITER=$((ITER + 1))
  echo "$ITER" > "$ITER_FILE"
  MODEL="${CLAUDE_MODEL:-haiku}"
  REASON="default cheap loop"
  if [[ -f "$FAIL_FILE" ]]; then
    MODEL="${CLAUDE_FALLBACK_MODEL:-sonnet}"
    REASON="previous loop failed"
    rm -f "$FAIL_FILE"
  elif [[ "${SONNET_REVIEW_EVERY_N:-0}" != "0" ]] && (( ITER % SONNET_REVIEW_EVERY_N == 0 )); then
    MODEL="${CLAUDE_FALLBACK_MODEL:-sonnet}"
    REASON="scheduled review every ${SONNET_REVIEW_EVERY_N} loops"
  fi
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] loop=$ITER model=$MODEL reason=$REASON" | tee -a _reports/freedommoney/claude_loop/control_room.log
  if scripts/freedom_claude_invoke_v2.sh "$MODEL"; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] loop=$ITER status=ok" | tee -a _reports/freedommoney/claude_loop/control_room.log
  else
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] loop=$ITER status=failed; next loop will use fallback" | tee -a _reports/freedommoney/claude_loop/control_room.log
    touch "$FAIL_FILE"
  fi
  sleep "${LOOP_SLEEP_SECONDS:-15}"
done
