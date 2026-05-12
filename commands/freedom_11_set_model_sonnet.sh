#!/usr/bin/env bash
set -euo pipefail
ROOT="${PROJECT_ROOT:-$(pwd)}"
mkdir -p "$ROOT/.agent"
if [[ ! -f "$ROOT/.agent/freedom_model.env" ]]; then
  cat > "$ROOT/.agent/freedom_model.env" <<'BASE'
CLAUDE_MODEL=haiku
CLAUDE_FALLBACK_MODEL=sonnet
SONNET_REVIEW_EVERY_N=5
BASE
fi
python3 - <<'PY'
from pathlib import Path
p=Path('.agent/freedom_model.env')
s=p.read_text() if p.exists() else ''
lines=[]; seen=False
for line in s.splitlines():
    if line.startswith('CLAUDE_MODEL='):
        lines.append('CLAUDE_MODEL=sonnet'); seen=True
    else:
        lines.append(line)
if not seen: lines.append('CLAUDE_MODEL=sonnet')
p.write_text('\n'.join(lines)+'\n')
PY
echo "CLAUDE_MODEL=sonnet"
