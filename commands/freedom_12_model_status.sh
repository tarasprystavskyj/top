#!/usr/bin/env bash
set -euo pipefail
ROOT="${PROJECT_ROOT:-$(pwd)}"
echo "== freedom_model.env =="
cat "$ROOT/.agent/freedom_model.env" 2>/dev/null || echo "missing"
echo
echo "== claude binary =="
which claude || true
claude --version 2>/dev/null || true
echo
echo "== relevant claude help =="
claude --help 2>/dev/null | grep -Ei -- '--model|--allowedTools|--dangerously|--permission|--print|-p' | head -50 || true
echo
echo "== last control logs =="
tail -n 30 "$ROOT/_reports/freedommoney/claude_loop/control_room.log" 2>/dev/null || true
