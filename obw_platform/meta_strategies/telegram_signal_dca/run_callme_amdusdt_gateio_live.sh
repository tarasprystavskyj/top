#!/usr/bin/env bash
set -euo pipefail

# Callme/AMDUSDT minimum-contract live profile using the Veronica live runner
# execution boundary. This intentionally does not read or print secrets; the
# runner loads the env file internally.

ACK_VALUE="I_ACCEPT_REAL_GATEIO_ORDERS"
if [[ "${CALLME_GATEIO_LIVE_ACK:-}" != "${ACK_VALUE}" ]]; then
  echo "Refusing to start live runner. Set CALLME_GATEIO_LIVE_ACK=${ACK_VALUE} explicitly." >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-/var/www/vps2.happyuser.info/top/backtest_SK/.venv38/bin/python}"
OUT_DIR="${OUT_DIR:-/var/www/vps2.happyuser.info/top/top_1/obw_platform/_reports/_live/callme_amdusdt_gateio_live_20260602}"
RUN_ID="${RUN_ID:-CALLME_AMDUSDT_GATEIO_LIVE_$(date -u +%Y%m%dT%H%M%SZ)}"
ENV_FILE="${ENV_FILE:-/var/www/vps2.happyuser.info/top/top_1/obw_platform/.env}"
SERVER_PROFILE="${SERVER_PROFILE:-/var/www/vps2.happyuser.info/top/.codex_CLI_laptop_local_philosofy/profiles/top_1.server.sh}"

mkdir -p "${OUT_DIR}"
if [[ -f "${SERVER_PROFILE}" ]]; then
  # shellcheck disable=SC1090
  source "${SERVER_PROFILE}"
fi

cd "${REPO_ROOT}"
exec "${PYTHON_BIN}" obw_platform/meta_strategies/telegram_signal_dca/hype_cap100_bingx_live_canary.py \
  --portfolio-id 4512404768792222208 \
  --symbol AMDUSDT \
  --live-symbol 'AMD/USDT:USDT' \
  --live-exchange-profile gateio_current \
  --env-file "${ENV_FILE}" \
  --meta-strategy-config-dir obw_platform/meta_strategies/telegram_signal_dca/meta_strategy_configs \
  --out-dir "${OUT_DIR}" \
  --state-path "${OUT_DIR}/state.json" \
  --status-path "${OUT_DIR}/RUN_STATUS.json" \
  --telemetry-path "${OUT_DIR}/telemetry.jsonl" \
  --session-db "${OUT_DIR}/session.sqlite" \
  --stdout-log-path "${OUT_DIR}/live_stdout.log" \
  --run-id "${RUN_ID}" \
  --initial-equity 30 \
  --initial-target-notional 30 \
  --max-gross-notional-usdt 30 \
  --max-one-side-notional-usdt 30 \
  --max-daily-loss-usdt 3 \
  --max-orders-per-hour 8 \
  --deadline-utc disabled \
  --order-error-backoff-sec 300 \
  --order-error-circuit-sec 1800 \
  --order-error-max-consecutive 2 \
  --entry-failure-cooldown-sec 3600 \
  --interval-sec 60 \
  --dca-eval-interval-sec 60 \
  --history-poll-interval-sec 60 \
  --loop
