#!/usr/bin/env bash
set -Eeuo pipefail

cd "${OBW_ROOT:-/var/www/vps2.happyuser.info/top/top_1}"

BASE_CFG="${BASE_CFG:-obw_platform/configs/V21_strict_trend_stable_live_static9p38.yaml}"
PLAN="${PLAN:-obw_platform/tuner_plans/tuner_plan_V21_live_candidates_1m_1y.py}"
LOG_DIR="${LOG_DIR:-_reports/v21_live_candidates_1m_1y_20260511}"
mkdir -p "$LOG_DIR" "obw_platform/universe" "DB" "obw_platform/configs"

IFS=' ' read -r -a SYMBOLS <<< "${SYMBOLS:-FREEDOMMONEY MAXXING CHECK}"
IFS=' ' read -r -a EXCHANGES <<< "${EXCHANGES:-bingx bybit gateio}"
TIMEFRAME="${TIMEFRAME:-1m}"
BACK_BARS="${BACK_BARS:-525600}"
JOBS="${JOBS:-1}"

run_fetch() {
  local raw="$1"
  local exchange="$2"
  local slug
  slug="$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '_')"
  local universe="obw_platform/universe/universe_${slug}_${TIMEFRAME}_${BACK_BARS}b.txt"
  local npz="DB/fast_cache_${TIMEFRAME}_${slug}_1y_${exchange}.npz"
  local fetch_log="${LOG_DIR}/${slug}_${exchange}_fetch.log"

  printf '%s\n' "$raw" > "$universe"
  if [[ -s "$npz" ]]; then
    echo "[$(date -Is)] ${raw}/${exchange}: existing NPZ -> $npz" | tee -a "$fetch_log"
    printf '%s\n' "$npz"
    return 0
  fi

  echo "[$(date -Is)] ${raw}/${exchange}: fetching ${TIMEFRAME} ${BACK_BARS} bars -> $npz" | tee "$fetch_log"
  set +e
  python3 obw_platform/scripts/fetch_backfill_ohlcv_npz_from_now_v1.py \
    -i "$universe" \
    -t "$TIMEFRAME" \
    --back-bars "$BACK_BARS" \
    --exchange "$exchange" \
    --ccxt-symbol-format usdtm \
    --npz-out "$npz" \
    --npz-only \
    --feature-set none \
    --cache-pack-trend \
    --debug 2>&1 | tee -a "$fetch_log"
  local fetch_status=${PIPESTATUS[0]}
  set -e

  if [[ "$fetch_status" -ne 0 || ! -s "$npz" ]]; then
    echo "[$(date -Is)] ${raw}/${exchange}: fetch failed status=${fetch_status}" | tee -a "$fetch_log"
    return 1
  fi

  printf '%s\n' "$npz"
}

run_tuner() {
  local raw="$1"
  local exchange="$2"
  local npz="$3"
  local slug
  slug="$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '_')"
  local prefix="V21_${slug}_${exchange}_${TIMEFRAME}_1y"
  local tuner_log="${LOG_DIR}/${slug}_${exchange}_tuner.log"
  local final_cfg="obw_platform/configs/V21_${slug}_${exchange}_live_candidate_${TIMEFRAME}_1y.yaml"

  echo "[$(date -Is)] ${raw}/${exchange}: tuning V21 -> $final_cfg" | tee "$tuner_log"
  set +e
  python3 obw_platform/auto_tuner_dual_fast_pack.py \
    --cfg "$BASE_CFG" \
    --npz "$npz" \
    --symbol "${raw}/USDT:USDT" \
    --plan "$PLAN" \
    --prefix "$prefix" \
    --jobs "$JOBS" \
    --min-trades 50 \
    --score-mode mtm \
    --w-pnl 500 \
    --w-mdd 90 \
    --w-realized-mdd 20 \
    --debug 2>&1 | tee -a "$tuner_log"
  local tuner_status=${PIPESTATUS[0]}
  set -e

  if [[ "$tuner_status" -ne 0 ]]; then
    echo "[$(date -Is)] ${raw}/${exchange}: tuner failed status=${tuner_status}" | tee -a "$tuner_log"
    return 1
  fi

  local session
  session="$(find "_reports/_auto_tuner_dual_fast_pack/$(basename "$PLAN" .py)" -maxdepth 1 -type d -name "${prefix}_*" -printf '%T@ %p\n' 2>/dev/null | sort -nr | awk 'NR==1{print $2}')"
  if [[ -n "${session:-}" && -s "$session/final_best.yaml" ]]; then
    cp "$session/final_best.yaml" "$final_cfg"
    echo "[$(date -Is)] ${raw}/${exchange}: final config copied -> $final_cfg" | tee -a "$tuner_log"
  else
    echo "[$(date -Is)] ${raw}/${exchange}: final_best.yaml not found" | tee -a "$tuner_log"
    return 1
  fi
}

status=0
python3 obw_platform/scripts/probe_exchange_connectivity_20260511.py | tee "${LOG_DIR}/connectivity_probe.json" || true

for raw in "${SYMBOLS[@]}"; do
  fetched_npz=""
  fetched_exchange=""
  for exchange in "${EXCHANGES[@]}"; do
    set +e
    fetch_output="$(run_fetch "$raw" "$exchange")"
    fetch_status=$?
    set -e
    if [[ "$fetch_status" -eq 0 ]]; then
      fetched_npz="$(printf '%s\n' "$fetch_output" | tail -n 1)"
      fetched_exchange="$exchange"
      break
    fi
  done
  if [[ -z "$fetched_npz" || ! -s "$fetched_npz" ]]; then
    echo "[$(date -Is)] ${raw}: all exchanges failed"
    status=1
    continue
  fi
  run_tuner "$raw" "$fetched_exchange" "$fetched_npz" || status=1
done

echo "[$(date -Is)] all done status=$status"
exit "$status"
