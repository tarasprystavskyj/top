#!/usr/bin/env bash
set -euo pipefail

ROOT="${PROJECT_ROOT:-/var/www/vps2.happyuser.info/top/top_1}"
RUNTIME_DIR="${SINGLE_AGENT_RUNTIME_DIR:-$ROOT/continuity/single_agent_loop_from_dex/runtime}"
SLEEP_SECONDS="${SINGLE_AGENT_SLEEP_SECONDS:-30}"
MAX_CYCLES="${SINGLE_AGENT_MAX_CYCLES:-0}"
INIT_ON_START="${SINGLE_AGENT_INIT_ON_START:-0}"

cd "$ROOT"
mkdir -p "$RUNTIME_DIR"

args=(--loop --auto-rotate --sleep "$SLEEP_SECONDS")
if [[ "$MAX_CYCLES" != "0" ]]; then
  args+=(--max-cycles "$MAX_CYCLES")
fi
if [[ "$INIT_ON_START" == "1" || ! -f "$RUNTIME_DIR/single_agent_state.json" ]]; then
  args=(--init "${args[@]}")
fi

exec python3 scripts/single_agent_loop.py "${args[@]}"
