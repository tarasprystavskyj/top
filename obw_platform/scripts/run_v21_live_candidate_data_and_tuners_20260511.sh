#!/usr/bin/env bash
set -Eeuo pipefail

cd /var/www/vps2.happyuser.info/top/top_1

BASE_CFG="obw_platform/configs/V21_strict_trend_stable_live_static9p38.yaml"
PLAN="obw_platform/tuner_plans/tuner_plan_V21_live_candidates_1m_1y.py"
LOG_DIR="_reports/v21_live_candidates_1m_1y_20260511"
mkdir -p "$LOG_DIR" "obw_platform/universe" "DB" "obw_platform/configs"

SYMBOLS=("FREEDOMMONEY" "MAXXING" "CHECK")

run_one() {
  local raw="$1"
  local slug
  slug="$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '_')"
  local universe="obw_platform/universe/universe_${slug}_1m_1y.txt"
  local npz="DB/fast_cache_1m_${slug}_1y_bingx.npz"
  local fetch_log="${LOG_DIR}/${slug}_fetch.log"
  local tuner_log="${LOG_DIR}/${slug}_tuner_launcher.log"
  local prefix="V21_${slug}_1m_1y"
  local final_cfg="obw_platform/configs/V21_${slug}_live_candidate_1m_1y.yaml"

  printf '%s\n' "$raw" > "$universe"

  echo "[$(date -Is)] ${raw}: start"
  if [[ ! -s "$npz" ]]; then
    echo "[$(date -Is)] ${raw}: fetching 1m 1y -> $npz"
    set +e
    python3 obw_platform/fetch_build_cache_and_fast_v1.py \
      -i "$universe" \
      -t 1m \
      --back-bars 525600 \
      --exchange bingx \
      --ccxt-symbol-format usdtm \
      --npz-out "$npz" \
      --npz-only \
      --feature-set none \
      --cache-pack-trend \
      --debug 2>&1 | tee "$fetch_log"
    local fetch_status=${PIPESTATUS[0]}
    set -e
    if [[ "$fetch_status" -ne 0 ]]; then
      echo "[$(date -Is)] ${raw}: fetch failed status=${fetch_status}" | tee -a "$fetch_log"
    fi
  else
    echo "[$(date -Is)] ${raw}: existing NPZ found -> $npz" | tee -a "$fetch_log"
  fi

  if [[ ! -s "$npz" ]]; then
    echo "[$(date -Is)] ${raw}: NPZ missing after fetch, skipping tuner" | tee -a "$fetch_log"
    return 1
  fi

  echo "[$(date -Is)] ${raw}: tuning V21 -> $final_cfg"
  set +e
  python3 obw_platform/auto_tuner_dual_fast_pack.py \
    --cfg "$BASE_CFG" \
    --npz "$npz" \
    --symbol "${raw}/USDT:USDT" \
    --plan "$PLAN" \
    --prefix "$prefix" \
    --jobs 1 \
    --min-trades 50 \
    --score-mode mtm \
    --w-pnl 500 \
    --w-mdd 90 \
    --w-realized-mdd 20 \
    --debug 2>&1 | tee "$tuner_log"
  local tuner_status=${PIPESTATUS[0]}
  set -e
  if [[ "$tuner_status" -ne 0 ]]; then
    echo "[$(date -Is)] ${raw}: tuner failed status=${tuner_status}" | tee -a "$tuner_log"
    return 1
  fi

  local session
  session="$(find _reports/_auto_tuner_dual_fast_pack/$(basename "$PLAN" .py) -maxdepth 1 -type d -name "${prefix}_*" -printf '%T@ %p\n' 2>/dev/null | sort -nr | awk 'NR==1{print $2}')"
  if [[ -n "${session:-}" && -s "$session/final_best.yaml" ]]; then
    cp "$session/final_best.yaml" "$final_cfg"
    echo "[$(date -Is)] ${raw}: final config copied -> $final_cfg"
  else
    echo "[$(date -Is)] ${raw}: final_best.yaml not found after tuner" >&2
    return 1
  fi
}

status=0
for sym in "${SYMBOLS[@]}"; do
  run_one "$sym" || status=1
done

echo "[$(date -Is)] all done status=$status"
exit "$status"
