#!/usr/bin/env bash
set -euo pipefail
ROOT="${PROJECT_ROOT:-$(pwd)}"
cd "$ROOT"
python3 obw_platform/tools/freedom_find_data_v1.py --root "$ROOT" --symbol FREEDOMMONEY || true
