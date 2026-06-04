#!/usr/bin/env bash
set -euo pipefail

# Callme multi-symbol BingX watcher/shadow follower. This script never submits
# exchange orders; it watches Callme source positions and BingX market data.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-/var/www/vps2.happyuser.info/top/backtest_SK/.venv38/bin/python}"
OUT_DIR="${OUT_DIR:-/var/www/vps2.happyuser.info/top/top_1/obw_platform/_reports/_shadow/callme_multi_bingx_54}"
CONFIG="${CONFIG:-obw_platform/meta_strategies/binance_online_copytrading/configs/bingx_friend_callme_multi_54.json}"
SERVER_PROFILE="${SERVER_PROFILE:-/var/www/vps2.happyuser.info/top/.codex_CLI_laptop_local_philosofy/profiles/top_1.server.sh}"

mkdir -p "${OUT_DIR}"
if [[ -f "${SERVER_PROFILE}" ]]; then
  # shellcheck disable=SC1090
  source "${SERVER_PROFILE}"
fi

cd "${REPO_ROOT}"
exec "${PYTHON_BIN}" obw_platform/meta_strategies/binance_online_copytrading/binance_online_copytrading.py \
  --config "${CONFIG}" \
  --paper-exchange bingx \
  --state-path "${OUT_DIR}/state.json" \
  --session-db "${OUT_DIR}/session.sqlite" \
  --shadow-orders-path "${OUT_DIR}/shadow_orders.jsonl" \
  --run-id "CALLME_MULTI_BINGX_SHADOW" \
  --interval-sec 60 \
  --loop
