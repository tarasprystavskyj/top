#!/usr/bin/env bash
set -euo pipefail
ROOT="${PROJECT_ROOT:-$(pwd)}"
ENV_FILE="$ROOT/.agent/freedom_relay.env"
if [[ -f "$ENV_FILE" ]]; then set -a; source "$ENV_FILE"; set +a; fi
SESSION="${TMUX_SESSION:-top_freedom_claude_loop}"
tmux kill-session -t "$SESSION" 2>/dev/null || true
echo "Stopped $SESSION if it existed."
