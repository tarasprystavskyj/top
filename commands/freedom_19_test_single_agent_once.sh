#!/usr/bin/env bash
set -euo pipefail
ROOT="${PROJECT_ROOT:-$(pwd)}"
cd "$ROOT"
scripts/freedom_claude_loop_single_agent_v3.sh
