#!/usr/bin/env bash
set -euo pipefail
ROOT="${PROJECT_ROOT:-$(pwd)}"
cd "$ROOT"
TS=$(date -u +%Y%m%dT%H%M%SZ)
OUT="/tmp/top_freedommoney_snapshot_${TS}.tar.gz"
tar -czf "$OUT" \
  docs/freedommoney_handoff \
  obw_platform/configs/*freedom* \
  _reports/freedommoney 2>/dev/null || tar -czf "$OUT" docs/freedommoney_handoff obw_platform/configs/*freedom* 2>/dev/null
sha256sum "$OUT" > "$OUT.sha256"
echo "$OUT"
echo "$OUT.sha256"
