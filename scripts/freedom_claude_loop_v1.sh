#!/usr/bin/env bash
set -euo pipefail
ROOT="${PROJECT_ROOT:-$(pwd)}"
ENV_FILE="$ROOT/.agent/freedom_relay.env"
if [[ -f "$ENV_FILE" ]]; then set -a; source "$ENV_FILE"; set +a; fi
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
PROMPT_FILE="$ROOT/docs/freedommoney_handoff/NEXT_AGENT_FIRST_PROMPT.md"
STATE_FILE="$ROOT/docs/freedommoney_handoff/AGENT_STATE.md"
LOG_DIR="$ROOT/_reports/freedommoney/claude_loop"
mkdir -p "$LOG_DIR"
cd "$ROOT"
TS=$(date -u +%Y%m%dT%H%M%SZ)
PROMPT=$(cat "$PROMPT_FILE"; printf '\n\nCurrent state:\n'; cat "$STATE_FILE" 2>/dev/null || true)
# Use timeout so one call cannot run forever.
timeout "$(( ${MAX_SESSION_MINUTES:-29} * 60 ))" "$CLAUDE_BIN" -p "$PROMPT" | tee "$LOG_DIR/claude_${TS}.log"
