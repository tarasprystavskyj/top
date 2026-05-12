#!/usr/bin/env bash
set -euo pipefail
ROOT="${PROJECT_ROOT:-$(pwd)}"
ENV_MAIN="$ROOT/.agent/freedom_relay.env"
ENV_MODEL="$ROOT/.agent/freedom_model.env"
[[ -f "$ENV_MAIN" ]] && { set -a; source "$ENV_MAIN"; set +a; }
[[ -f "$ENV_MODEL" ]] && { set -a; source "$ENV_MODEL"; set +a; }
cd "$ROOT"
mkdir -p _reports/freedommoney/claude_loop docs/freedommoney_handoff
ITER_FILE="docs/freedommoney_handoff/LOOP_ITERATION.txt"
ITER=0
[[ -f "$ITER_FILE" ]] && ITER=$(cat "$ITER_FILE" 2>/dev/null || echo 0)
MODEL="${CLAUDE_MODEL:-haiku}"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] single-agent control room started model=$MODEL" | tee -a _reports/freedommoney/claude_loop/control_room.log
while true; do
  ITER=$((ITER + 1))
  echo "$ITER" > "$ITER_FILE"
  MODEL="${CLAUDE_MODEL:-haiku}"
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] loop=$ITER model=$MODEL mode=single-agent" | tee -a _reports/freedommoney/claude_loop/control_room.log
  if scripts/freedom_claude_loop_single_agent_v3.sh; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] loop=$ITER status=ok" | tee -a _reports/freedommoney/claude_loop/control_room.log
  else
    code=$?
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] loop=$ITER status=failed exit=$code; retrying same model" | tee -a _reports/freedommoney/claude_loop/control_room.log
  fi
  sleep "${LOOP_SLEEP_SECONDS:-30}"
done
