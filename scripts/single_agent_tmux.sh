#!/usr/bin/env bash
set -euo pipefail

ROOT="${PROJECT_ROOT:-/var/www/vps2.happyuser.info/top/top_1}"
SESSION="${SINGLE_AGENT_TMUX_SESSION:-top1_single_agent}"
RUNTIME_DIR="${SINGLE_AGENT_RUNTIME_DIR:-$ROOT/continuity/single_agent_loop_from_dex/runtime}"
LOG_DIR="${SINGLE_AGENT_LOG_DIR:-$RUNTIME_DIR/logs}"
LOG_FILE="$LOG_DIR/control_room.log"

usage() {
  cat <<'EOF'
Usage: scripts/single_agent_tmux.sh start|status|stop|tail

Environment:
  SINGLE_AGENT_TMUX_SESSION   tmux session name, default top1_single_agent
  SINGLE_AGENT_RUNTIME_DIR    runtime/state/tree directory
  SINGLE_AGENT_LOG_DIR        log directory, default $SINGLE_AGENT_RUNTIME_DIR/logs
  SINGLE_AGENT_MAX_CYCLES     optional bounded run cap
  SINGLE_AGENT_SLEEP_SECONDS  loop sleep seconds
  SINGLE_AGENT_INIT_ON_START  set 1 to reinitialize state on start
EOF
}

alive() {
  tmux has-session -t "$SESSION" 2>/dev/null
}

status_json() {
  python3 - "$RUNTIME_DIR" "$SESSION" <<'PY'
import glob
import json
import os
import sys

runtime = sys.argv[1]
session = sys.argv[2]
tree_path = os.path.join(runtime, "ui_data", "single_agent_tree.json")
print("session=%s" % session)
print("runtime=%s" % runtime)
print("tree=%s" % tree_path)
if os.path.exists(tree_path):
    with open(tree_path, encoding="utf-8-sig") as fh:
        tree = json.load(fh)
    summary = tree.get("summary", {})
    print("tree_schema=%s" % tree.get("schema_version", ""))
    print(
        "cycle=%s jobs=%s running=%s queued=%s next_action=%s"
        % (
            summary.get("cycle"),
            summary.get("job_count"),
            summary.get("running_jobs"),
            summary.get("queued_jobs"),
            summary.get("next_action"),
        )
    )
else:
    print("tree_schema=missing")

results = sorted(glob.glob(os.path.join(runtime, "jobs", "*.result.json")))[-5:]
if results:
    print("recent_results:")
    for path in results:
        try:
            with open(path, encoding="utf-8-sig") as fh:
                result = json.load(fh)
            print(
                "- %s %s %s"
                % (
                    result.get("kind"),
                    result.get("status"),
                    os.path.relpath(path, runtime),
                )
            )
        except Exception as exc:
            print("- unreadable %s: %s" % (path, exc))
else:
    print("recent_results=none")
PY
}

cmd="${1:-}"
case "$cmd" in
  start)
    mkdir -p "$LOG_DIR"
    if alive; then
      echo "already_running session=$SESSION"
      status_json
      exit 0
    fi
    tmux new-session -d -s "$SESSION" \
      "cd '$ROOT' && SINGLE_AGENT_RUNTIME_DIR='$RUNTIME_DIR' SINGLE_AGENT_LOG_DIR='$LOG_DIR' SINGLE_AGENT_MAX_CYCLES='${SINGLE_AGENT_MAX_CYCLES:-0}' SINGLE_AGENT_SLEEP_SECONDS='${SINGLE_AGENT_SLEEP_SECONDS:-30}' SINGLE_AGENT_INIT_ON_START='${SINGLE_AGENT_INIT_ON_START:-0}' scripts/single_agent_control_room.sh >> '$LOG_FILE' 2>&1"
    echo "started session=$SESSION log=$LOG_FILE"
    ;;
  status)
    if alive; then
      echo "tmux_alive=yes"
    else
      echo "tmux_alive=no"
    fi
    status_json
    ;;
  stop)
    if alive; then
      tmux kill-session -t "$SESSION"
      echo "stopped session=$SESSION"
    else
      echo "not_running session=$SESSION"
    fi
    ;;
  tail)
    if [[ -f "$LOG_FILE" ]]; then
      tail -n "${TAIL_LINES:-80}" "$LOG_FILE"
    else
      echo "log_missing $LOG_FILE"
    fi
    ;;
  ""|-h|--help|help)
    usage
    ;;
  *)
    usage
    exit 2
    ;;
esac
