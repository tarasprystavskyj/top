#!/usr/bin/env bash
set -euo pipefail
ROOT="${PROJECT_ROOT:-$(pwd)}"
ENV_FILE="$ROOT/.agent/freedom_relay.env"
if [[ -f "$ENV_FILE" ]]; then set -a; source "$ENV_FILE"; set +a; fi
cd "$ROOT"
mkdir -p _reports/freedommoney/claude_loop
while true; do
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] starting Claude loop iteration"
  scripts/freedom_claude_loop_v1.sh || true
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] iteration ended; sleeping 60s"
  sleep 60
done
