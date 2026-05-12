#!/usr/bin/env bash
set -euo pipefail
ROOT="${PROJECT_ROOT:-$(pwd)}"
ENV_FILE="$ROOT/.agent/freedom_relay.env"
[[ -f "$ENV_FILE" ]] && { set -a; source "$ENV_FILE"; set +a; }
SESSION="${TMUX_SESSION:-top_freedom_claude_loop}"
tmux has-session -t "$SESSION" 2>/dev/null && { echo "tmux session exists: $SESSION"; echo "Attach: tmux attach -t $SESSION"; exit 0; }
tmux new-session -d -s "$SESSION" "cd '$ROOT' && scripts/freedom_control_room_single_agent_v3.sh"
echo "Started tmux session: $SESSION"
echo "Attach: tmux attach -t $SESSION"
