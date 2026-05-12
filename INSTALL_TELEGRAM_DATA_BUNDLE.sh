#!/usr/bin/env bash
set -euo pipefail
ROOT="${1:-/var/www/vps2.happyuser.info/top/top_1}"
cd "$ROOT"
mkdir -p obw_platform/telegram_signal_tools universe docs runs/telegram_signal_bt DB
chmod +x obw_platform/telegram_signal_tools/*.py 2>/dev/null || true
cat <<'EOF'
Installed telegram signal data bundle files.
Next:
  source .venv38/bin/activate
  pip install -U ccxt pandas matplotlib
  bash -lc 'sed -n "1,220p" docs/TELEGRAM_SIGNAL_DATA_BUNDLE_README.md'
EOF
