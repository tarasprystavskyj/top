#!/usr/bin/env bash
set -euo pipefail
ROOT="/var/www/vps2.happyuser.info/top/top_1"
PROMPT="$ROOT/continuity/single_agent_loop_from_dex/CODEX_RESUME_PROMPT_20260512.md"
cd "$ROOT"
exec codex resume 019e16d3-832e-7840-a144-f9fd62aef18d --ask-for-approval on-request --sandbox danger-full-access --no-alt-screen "$(cat "$PROMPT")"
