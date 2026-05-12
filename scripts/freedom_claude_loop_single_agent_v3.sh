#!/usr/bin/env bash
set -euo pipefail
ROOT="${PROJECT_ROOT:-$(pwd)}"
ENV_MAIN="$ROOT/.agent/freedom_relay.env"
ENV_MODEL="$ROOT/.agent/freedom_model.env"
[[ -f "$ENV_MAIN" ]] && { set -a; source "$ENV_MAIN"; set +a; }
[[ -f "$ENV_MODEL" ]] && { set -a; source "$ENV_MODEL"; set +a; }
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
MODEL="${CLAUDE_MODEL:-haiku}"
PROMPT_FILE="${PROMPT_FILE:-$ROOT/docs/freedommoney_handoff/NEXT_AGENT_FIRST_PROMPT.md}"
STATE_FILE="${STATE_FILE:-$ROOT/docs/freedommoney_handoff/AGENT_STATE.md}"
LOG_DIR="${LOG_DIR:-$ROOT/_reports/freedommoney/claude_loop}"
mkdir -p "$LOG_DIR" "$ROOT/docs/freedommoney_handoff"
cd "$ROOT"
TS=$(date -u +%Y%m%dT%H%M%SZ)
OUT_LOG="$LOG_DIR/claude_single_${MODEL}_${TS}.log"
PROMPT_TMP="$LOG_DIR/prompt_${TS}.md"
{
  cat "$PROMPT_FILE"
  printf '\n\n# Current AGENT_STATE.md\n'
  cat "$STATE_FILE" 2>/dev/null || true
  printf '\n\n# Loop metadata\n'
  printf 'Current UTC: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'Model: %s\n' "$MODEL"
  printf 'Runtime limit minutes: %s\n' "${MAX_SESSION_MINUTES:-29}"
} > "$PROMPT_TMP"

HELP_TXT="$($CLAUDE_BIN --help 2>/dev/null || true)"
ARGS=()
if printf '%s' "$HELP_TXT" | grep -q -- '--model'; then
  ARGS+=(--model "$MODEL")
fi
# Keep a single model. No fallback model is passed.
# Permit autonomous command execution if the installed Claude Code supports it.
if [[ "${CLAUDE_SKIP_PERMISSIONS:-1}" == "1" ]] && printf '%s' "$HELP_TXT" | grep -q -- '--dangerously-skip-permissions'; then
  ARGS+=(--dangerously-skip-permissions)
elif [[ "${CLAUDE_SKIP_PERMISSIONS:-1}" == "1" ]] && printf '%s' "$HELP_TXT" | grep -q -- '--permission-mode'; then
  ARGS+=(--permission-mode bypassPermissions)
fi
if printf '%s' "$HELP_TXT" | grep -q -- '--allowedTools'; then
  ARGS+=(--allowedTools "${CLAUDE_ALLOWED_TOOLS:-Bash,Read,Write,Edit,MultiEdit,Glob,Grep,LS}")
fi

export ANTHROPIC_MODEL="$MODEL"
export CLAUDE_MODEL="$MODEL"

echo "[invoke] model=$MODEL args=${ARGS[*]:-<none>} prompt=$PROMPT_TMP" | tee "$OUT_LOG"
set +e
# -p/--print keeps the same single Claude Code agent but runs one bounded autonomous turn.
# It can still use tools when permissions/tool flags allow it.
timeout "$(( ${MAX_SESSION_MINUTES:-29} * 60 ))" "$CLAUDE_BIN" "${ARGS[@]}" -p "$(cat "$PROMPT_TMP")" 2>&1 | tee -a "$OUT_LOG"
STATUS=${PIPESTATUS[0]}
set -e

# Guard: a loop that only asks permission or only plans is a failed loop.
if grep -Eiq 'May I proceed|Shall I proceed|Should I proceed|permission to|please confirm|waiting for confirmation|I need permission' "$OUT_LOG"; then
  echo "[guard] failed: model asked for permission instead of executing" | tee -a "$OUT_LOG"
  exit 42
fi
if grep -Eiq 'ACTIONS_EXECUTED:[[:space:]]*0' "$OUT_LOG"; then
  echo "[guard] failed: ACTIONS_EXECUTED=0" | tee -a "$OUT_LOG"
  exit 43
fi
exit "$STATUS"
