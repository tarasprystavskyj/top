#!/usr/bin/env bash
set -euo pipefail

# Callme/AMD HTX live profile using the guarded canary runner.
# The runner reads the env file internally and must not print secret values.

ACK_VALUE="I_ACCEPT_REAL_HTX_ORDERS"
if [[ "${CALLME_AMD_HTX_LIVE_ACK:-}" != "${ACK_VALUE}" ]]; then
  echo "Refusing to start live runner. Set CALLME_AMD_HTX_LIVE_ACK=${ACK_VALUE} explicitly." >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-/var/www/vps2.happyuser.info/top/backtest_SK/.venv38/bin/python}"
CFG="${CFG:-${REPO_ROOT}/obw_platform/meta_strategies/telegram_signal_dca/configs/htx_callme_amd_live_90.json}"
OUT_DIR="${OUT_DIR:-/var/www/vps2.happyuser.info/top/top_1/obw_platform/_reports/_live/callme_amdusdt_htx_live_$(date -u +%Y%m%dT%H%M%SZ)}"
RUN_ID="${RUN_ID:-CALLME_AMD_HTX_LIVE_90_$(date -u +%Y%m%dT%H%M%SZ)}"
SERVER_PROFILE="${SERVER_PROFILE:-/var/www/vps2.happyuser.info/top/.codex_CLI_laptop_local_philosofy/profiles/top_1.server.sh}"
DEADLINE_UTC="${CALLME_AMD_HTX_DEADLINE_UTC:-}"

mkdir -p "${OUT_DIR}"
if [[ -f "${SERVER_PROFILE}" ]]; then
  # shellcheck disable=SC1090
  source "${SERVER_PROFILE}"
fi

cd "${REPO_ROOT}"
exec "${PYTHON_BIN}" obw_platform/meta_strategies/telegram_signal_dca/hype_cap100_bingx_live_canary.py \
  --live-config "${CFG}" \
  --out-dir "${OUT_DIR}" \
  --state-path "${OUT_DIR}/state.json" \
  --status-path "${OUT_DIR}/RUN_STATUS.json" \
  --telemetry-path "${OUT_DIR}/telemetry.jsonl" \
  --session-db "${OUT_DIR}/session.sqlite" \
  --stdout-log-path "${OUT_DIR}/live_stdout.log" \
  --run-id "${RUN_ID}" \
  --deadline-utc "${DEADLINE_UTC}" \
  --loop
