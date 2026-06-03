#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python}"
OUT_DIR="${OUT_DIR:-obw_platform/meta_strategies/binance_online_copytrading/reports/htx_friend_200usd_shadow}"
CONFIG="${CONFIG:-obw_platform/meta_strategies/binance_online_copytrading/configs/htx_friend_200usd_55_45.json}"

cd "${REPO_ROOT}"
exec "${PYTHON_BIN}" obw_platform/meta_strategies/binance_online_copytrading/binance_online_copytrading.py \
  --config "${CONFIG}" \
  --paper-exchange htx \
  --state-path "${OUT_DIR}/state.json" \
  --session-db "${OUT_DIR}/session.sqlite" \
  --shadow-orders-path "${OUT_DIR}/shadow_orders.jsonl" \
  --run-id "FRIEND_200USD_HTX_SHADOW_ONCE" \
  --once
