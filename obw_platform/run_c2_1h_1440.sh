#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
python3 backtester_core.py --cfg configs/cs_C2_base_1h.yaml --limit-bars 1440
python3 verify_c2_result.py
