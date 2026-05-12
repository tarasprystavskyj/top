#!/usr/bin/env bash
set -euo pipefail

ROOT="/var/www/vps2.happyuser.info/top/top_1"
LANE="obw_platform/meta_strategies/akela_meta_short"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="$ROOT/_reports/akela_meta_short/champion_${STAMP}"
SUMMARY="$ROOT/$LANE/reports/latest_champion_search.md"

TUNER_MAX_SECONDS="${AKELA_TUNER_MAX_SECONDS:-7200}"
TUNER_JOBS="${AKELA_TUNER_JOBS:-1}"
TUNER_MIN_TRADES="${AKELA_TUNER_MIN_TRADES:-50}"

mkdir -p "$RUN_DIR" "$ROOT/$LANE/reports"
cd "$ROOT"

SYMBOLS=(
  "IDOL|IDOL/USDT:USDT|DB/akela_meta_short_1m_1y_idol_bingx.npz|obw_platform/configs/V21_freedommoney_bingx_live_candidate_1m_1y.yaml"
  "FREEDOMMONEY|FREEDOMMONEY/USDT:USDT|DB/fast_cache_1m_freedommoney_1y_bingx.npz|obw_platform/configs/V21_current_best_tuner_freedommoney_bingx_1m_1y_20260511.yaml"
  "MAXXING|MAXXING/USDT:USDT|DB/fast_cache_1m_maxxing_1y_bingx.npz|obw_platform/configs/V21_maxxing_bingx_live_candidate_1m_1y.yaml"
  "SUP|SUP/USDT:USDT|DB/akela_meta_short_1m_1y_sup_bingx.npz|obw_platform/configs/V21_freedommoney_bingx_live_candidate_1m_1y.yaml"
)

CONFIGS=(
  "obw_platform/configs/V21_freedommoney_bingx_live_candidate_1m_1y.yaml"
  "obw_platform/configs/V21_maxxing_bingx_live_candidate_1m_1y.yaml"
  "obw_platform/configs/V21_current_best_tuner_freedommoney_bingx_1m_1y_20260511.yaml"
  "obw_platform/configs/V21_strict_trend_stable_live_static9p38.yaml"
)

write_header() {
  {
    echo "# Akela Yearly Champion Search"
    echo
    echo "Updated: ${STAMP}"
    echo "Run dir: \`_reports/akela_meta_short/champion_${STAMP}\`"
    echo
    echo "## Objective"
    echo
    echo "Find a new candidate champion for paper live using existing V21 backtester/tuner only."
    echo
    echo "## Guardrails"
    echo
    echo "- Existing backtester only: \`obw_platform/backtester_dual_long_short_fast_pack_v2.py\`."
    echo "- Existing tuner only: \`obw_platform/auto_tuner_dual_fast_pack.py\`."
    echo "- Existing tuning plan only: \`obw_platform/tuner_plans/tuner_plan_V21_live_candidates_1m_1y.py\`."
    echo "- No live/deploy changes."
    echo "- No production YAML edits."
    echo "- No exchange, fee, slippage, liquidation, or backtest math changes."
    echo
    echo "## Yearly Backtest Matrix"
    echo
    echo "| symbol | config | status | log |"
    echo "| --- | --- | --- | --- |"
  } > "$SUMMARY"
}

append_line() {
  printf '%s\n' "$1" >> "$SUMMARY"
}

safe_slug() {
  printf '%s' "$1" | tr '/:. ' '____' | tr -cd 'A-Za-z0-9_-'
}

write_header

for item in "${SYMBOLS[@]}"; do
  IFS='|' read -r key symbol npz start_cfg <<< "$item"
  for cfg in "${CONFIGS[@]}"; do
    cfg_name="$(basename "$cfg" .yaml)"
    sym_slug="$(safe_slug "$key")"
    out_dir="$RUN_DIR/backtests/${sym_slug}/${cfg_name}"
    mkdir -p "$out_dir"
    log="$out_dir/backtest.log"
    status="ok"
    if [[ ! -f "$npz" ]]; then
      status="missing_npz"
      echo "[skip] missing npz $npz" > "$log"
    elif [[ ! -f "$cfg" ]]; then
      status="missing_cfg"
      echo "[skip] missing cfg $cfg" > "$log"
    else
      if ! python3 obw_platform/backtester_dual_long_short_fast_pack_v2.py \
        --cfg "$cfg" \
        --npz "$npz" \
        --symbol "$symbol" \
        --plots "$out_dir" \
        --export-curves "$out_dir/curves.csv" > "$log" 2>&1; then
        status="failed"
      fi
    fi
    append_line "| \`$symbol\` | \`$cfg_name\` | $status | \`${log#$ROOT/}\` |"
  done
done

append_line ""
append_line "## Night Tuning"
append_line ""
append_line "| symbol | start cfg | status | tuner summary | log |"
append_line "| --- | --- | --- | --- | --- |"

for item in "${SYMBOLS[@]}"; do
  IFS='|' read -r key symbol npz start_cfg <<< "$item"
  sym_slug="$(safe_slug "$key")"
  log="$RUN_DIR/tuner_${sym_slug}.log"
  status="ok"
  summary_path=""
  if [[ ! -f "$npz" ]]; then
    status="missing_npz"
    echo "[skip] missing npz $npz" > "$log"
  elif [[ ! -f "$start_cfg" ]]; then
    status="missing_cfg"
    echo "[skip] missing cfg $start_cfg" > "$log"
  else
    prefix="akela_${sym_slug}_v21_1y"
    if ! python3 obw_platform/auto_tuner_dual_fast_pack.py \
      --cfg "$start_cfg" \
      --npz "$npz" \
      --symbol "$symbol" \
      --plan obw_platform/tuner_plans/tuner_plan_V21_live_candidates_1m_1y.py \
      --prefix "$prefix" \
      --jobs "$TUNER_JOBS" \
      --min-trades "$TUNER_MIN_TRADES" \
      --score-mode mtm \
      --max-seconds "$TUNER_MAX_SECONDS" \
      --debug > "$log" 2>&1; then
      status="failed"
    fi
    summary_path="$(python3 - "$log" <<'PY'
import json, sys
text=open(sys.argv[1], encoding='utf-8', errors='replace').read()
start=text.rfind('{')
if start >= 0:
    try:
        obj=json.loads(text[start:])
        print(obj.get('session_dir', '') + '/tuner_summary.json')
    except Exception:
        print('')
PY
)"
  fi
  append_line "| \`$symbol\` | \`$(basename "$start_cfg" .yaml)\` | $status | \`${summary_path}\` | \`${log#$ROOT/}\` |"
done

append_line ""
append_line "## Next Decision"
append_line ""
append_line "Review tuner summaries and yearly backtest logs. A paper-live candidate must have positive MTM score, controlled MTM drawdown, no margin-call events, and no single-symbol tail concentration."

cp "$SUMMARY" "$RUN_DIR/latest_champion_search.md"
echo "[done] summary=$SUMMARY run_dir=$RUN_DIR"
