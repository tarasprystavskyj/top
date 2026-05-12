#!/usr/bin/env bash
set -euo pipefail
ROOT="${PROJECT_ROOT:-$(pwd)}"
ENV_MAIN="$ROOT/.agent/freedom_relay.env"
ENV_MODEL="$ROOT/.agent/freedom_model.env"
if [[ -f "$ENV_MAIN" ]]; then set -a; source "$ENV_MAIN"; set +a; fi
if [[ -f "$ENV_MODEL" ]]; then set -a; source "$ENV_MODEL"; set +a; fi
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
MODEL="${1:-${CLAUDE_MODEL:-haiku}}"
shift || true
PROMPT_FILE="${PROMPT_FILE:-$ROOT/docs/freedommoney_handoff/NEXT_AGENT_FIRST_PROMPT.md}"
STATE_FILE="${STATE_FILE:-$ROOT/docs/freedommoney_handoff/AGENT_STATE.md}"
LOG_DIR="${LOG_DIR:-$ROOT/_reports/freedommoney/claude_loop}"
mkdir -p "$LOG_DIR"
cd "$ROOT"
TS=$(date -u +%Y%m%dT%H%M%SZ)
PROMPT=$(cat "$PROMPT_FILE"; printf '\n\nCurrent state:\n'; cat "$STATE_FILE" 2>/dev/null || true; printf '\n\nModel policy:\nUse cheap model for routine shell/data/report work. Escalate only for code changes or failures. Current model: %s\n' "$MODEL")
export ANTHROPIC_MODEL="$MODEL"
export CLAUDE_MODEL="$MODEL"
HELP_TXT="$($CLAUDE_BIN --help 2>/dev/null || true)"
if printf '%s' "$HELP_TXT" | grep -q -- '--model'; then
  timeout "$(( ${MAX_SESSION_MINUTES:-29} * 60 ))" "$CLAUDE_BIN" --model "$MODEL" -p "$PROMPT" "$@" | tee "$LOG_DIR/claude_${MODEL}_${TS}.log"
else
  echo "[warn] Claude CLI help does not show --model; using ANTHROPIC_MODEL=$MODEL only" | tee "$LOG_DIR/claude_${MODEL}_${TS}.log"
  timeout "$(( ${MAX_SESSION_MINUTES:-29} * 60 ))" "$CLAUDE_BIN" -p "$PROMPT" "$@" | tee -a "$LOG_DIR/claude_${MODEL}_${TS}.log"
fi
