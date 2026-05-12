#!/usr/bin/env bash
set -euo pipefail
ROOT="${PROJECT_ROOT:-$(pwd)}"
cd "$ROOT"
echo "ROOT=$ROOT"
echo "Python:"; python3 --version || true
echo "Claude:"; command -v claude || true
echo "Configs:"; ls -lah obw_platform/configs/*freedom* obw_platform/configs/h4_c002_w9_fee_0002.yaml 2>/dev/null || true
echo "Handoff docs:"; ls -lah docs/freedommoney_handoff || true
echo "Recent reports:"; find _reports/freedommoney -maxdepth 2 -type f 2>/dev/null | tail -30 || true
