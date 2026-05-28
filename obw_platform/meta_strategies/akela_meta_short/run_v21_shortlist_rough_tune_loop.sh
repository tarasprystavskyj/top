#!/usr/bin/env bash
set -euo pipefail

ROOT="/var/www/vps2.happyuser.info/top/top_1"
LANE="$ROOT/obw_platform/meta_strategies/akela_meta_short"
RANK_CSV="${AKELA_ROUGH_TUNE_RANK_CSV:-$ROOT/_reports/akela_meta_short/v21_midcaps_rank/v21_majors_rank.csv}"
NPZ="${AKELA_ROUGH_TUNE_NPZ:-$ROOT/_reports/akela_meta_short/v21_midcaps_rank/majors_bingx_5m_1500b.npz}"
BASE_CFG="${AKELA_ROUGH_TUNE_BASE_CFG:-$ROOT/obw_platform/meta_strategies/akela_meta_short/live_freedommoney/V21_freedommoney_bingx_live_min2p2.yaml}"
PLAN="${AKELA_ROUGH_TUNE_PLAN:-$ROOT/obw_platform/tuner_plans/tuner_plan_V21_live_candidates_1m_1y.py}"
PLAN_STEM="$(basename "$PLAN" .py)"
OUT_ROOT="${AKELA_ROUGH_TUNE_OUT_ROOT:-$ROOT/_reports/akela_meta_short/v21_shortlist_rough_tune}"
SUMMARY="$LANE/reports/latest_v21_shortlist_rough_tune.md"

MAX_SYMBOLS="${AKELA_ROUGH_TUNE_MAX_SYMBOLS:-10}"
MAX_SECONDS_PER_SYMBOL="${AKELA_ROUGH_TUNE_MAX_SECONDS_PER_SYMBOL:-1800}"
JOBS="${AKELA_ROUGH_TUNE_JOBS:-1}"
MIN_TRADES="${AKELA_ROUGH_TUNE_MIN_TRADES:-50}"
SLEEP_SEC="${AKELA_ROUGH_TUNE_SLEEP_SEC:-21600}"
MIN_PASSIVE_N="${AKELA_ROUGH_TUNE_MIN_PASSIVE_N:-100}"
MAX_SPREAD_P95_BP="${AKELA_ROUGH_TUNE_MAX_SPREAD_P95_BP:-12}"
MAX_ROUNDTRIP_FLOOR_P50_BP="${AKELA_ROUGH_TUNE_MAX_ROUNDTRIP_FLOOR_P50_BP:-35}"
MIN_TOP10_SIDE_USDT="${AKELA_ROUGH_TUNE_MIN_TOP10_SIDE_USDT:-10000}"
MAX_TAIL_ABS="${AKELA_ROUGH_TUNE_MAX_TAIL_ABS:-2}"

mkdir -p "$OUT_ROOT" "$LANE/reports"
cd "$ROOT"

safe_slug() {
  printf '%s' "$1" | tr '/:. ' '____' | tr -cd 'A-Za-z0-9_-'
}

while true; do
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  run_dir="$OUT_ROOT/$stamp"
  mkdir -p "$run_dir/cfg" "$run_dir/logs"

  python3 - "$RANK_CSV" "$BASE_CFG" "$run_dir" "$MAX_SYMBOLS" "$MIN_PASSIVE_N" "$MAX_SPREAD_P95_BP" "$MAX_ROUNDTRIP_FLOOR_P50_BP" "$MIN_TOP10_SIDE_USDT" "$MAX_TAIL_ABS" <<'PY'
import sys
from pathlib import Path

import pandas as pd
import yaml

rank_csv = Path(sys.argv[1])
base_cfg = Path(sys.argv[2])
run_dir = Path(sys.argv[3])
max_symbols = int(sys.argv[4])
min_passive_n = int(float(sys.argv[5]))
max_spread_p95_bp = float(sys.argv[6])
max_roundtrip_floor_p50_bp = float(sys.argv[7])
min_top10_side_usdt = float(sys.argv[8])
max_tail_abs = float(sys.argv[9])

df = pd.read_csv(rank_csv)
scenario_order = {"passive_spread_p95": 0, "passive_spread_p50": 1, "cfg_static": 2}
df["scenario_rank"] = df["scenario"].map(scenario_order).fillna(9)
df = df[
    (df["scenario"] == "passive_spread_p95")
    & (df["margin_call_events_total"].fillna(999).astype(float) == 0)
].copy()
df["tail_abs"] = df["terminal_unrealized_to_realized_ratio"].fillna(0).astype(float).abs()
df = df[
    (df["tail_abs"] <= max_tail_abs)
    & (df["passive_n"].fillna(0).astype(float) >= min_passive_n)
    & (df["spread_p95_bp"].fillna(999).astype(float) <= max_spread_p95_bp)
    & (df["roundtrip_floor_p50_bp"].fillna(999).astype(float) <= max_roundtrip_floor_p50_bp)
    & (df["top10_bid_p50_usdt"].fillna(0).astype(float) >= min_top10_side_usdt)
    & (df["top10_ask_p50_usdt"].fillna(0).astype(float) >= min_top10_side_usdt)
].copy()
df = df.sort_values(["score", "return_mtm_pct_on_start"], ascending=[False, False])

base = yaml.safe_load(base_cfg.read_text(encoding="utf-8")) or {}
rows = []
seen = set()
for _, row in df.iterrows():
    sym = str(row["symbol"])
    if sym in seen:
        continue
    seen.add(sym)
    cfg = dict(base)
    cfg = yaml.safe_load(yaml.safe_dump(base, sort_keys=False)) or {}
    cfg["symbol"] = sym
    cfg["cache_db"] = ""
    slip = float(row.get("static_slippage_bp") or row.get("spread_p95_bp") or row.get("spread_p50_bp") or 9.38)
    cfg.setdefault("backtest", {}).setdefault("slippage", {})
    cfg["backtest"]["slippage"].update({"enabled": True, "mode": "static", "static_bp": slip})
    slug = sym.replace("/", "_").replace(":", "_")
    cfg_path = run_dir / "cfg" / f"{slug}_rough_tune_start.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    rows.append({
        "symbol": sym,
        "slug": slug,
        "cfg_path": str(cfg_path),
        "score": float(row.get("score") or 0.0),
        "return_mtm_pct_on_start": row.get("return_mtm_pct_on_start"),
        "mdd_mtm_%": row.get("mdd_mtm_%"),
        "trades_total": row.get("trades_total"),
        "static_slippage_bp": slip,
        "spread_p50_bp": row.get("spread_p50_bp"),
        "spread_p95_bp": row.get("spread_p95_bp"),
        "roundtrip_floor_p50_bp": row.get("roundtrip_floor_p50_bp"),
        "passive_n": row.get("passive_n"),
        "top10_bid_p50_usdt": row.get("top10_bid_p50_usdt"),
        "top10_ask_p50_usdt": row.get("top10_ask_p50_usdt"),
        "terminal_unrealized_to_realized_ratio": row.get("terminal_unrealized_to_realized_ratio"),
    })
    if len(rows) >= max_symbols:
        break

pd.DataFrame(rows).to_csv(run_dir / "shortlist.csv", index=False)
PY

  {
    echo "# V21 Shortlist Rough Tune"
    echo
    echo "Updated: $stamp"
    echo "Run dir: \`${run_dir#$ROOT/}\`"
    echo
    echo "## Guardrails"
    echo
    echo "- Existing tuner only: \`obw_platform/auto_tuner_dual_fast_pack.py\`."
    echo "- Existing backtester path inside tuner only."
    echo "- No live/deploy actions."
    echo "- No production YAML edits; generated YAML is under \`_reports\`."
    echo "- Goal: find configs with \`margin_call_events_total = 0\`, controlled MTM MDD, controlled terminal tail."
    echo "- Pre-tune liquidity gate: passive_n >= $MIN_PASSIVE_N, spread_p95 <= ${MAX_SPREAD_P95_BP}bp, roundtrip_floor_p50 <= ${MAX_ROUNDTRIP_FLOOR_P50_BP}bp, top10 bid/ask >= ${MIN_TOP10_SIDE_USDT} USDT, abs(tail) <= $MAX_TAIL_ABS."
    echo
    echo "## Jobs"
    echo
    echo "| symbol | rank score | start slippage bp | status | tuner summary | log |"
    echo "| --- | ---: | ---: | --- | --- | --- |"
  } > "$SUMMARY"

  if [[ ! -s "$run_dir/shortlist.csv" ]]; then
    echo "| n/a | 0 | 0 | empty_shortlist |  |  |" >> "$SUMMARY"
  else
    tail -n +2 "$run_dir/shortlist.csv" | while IFS=',' read -r symbol slug cfg_path score ret mdd trades slip spread50 spread95 tail_ratio; do
      log="$run_dir/logs/${slug}_tuner.log"
      prefix="akela_${slug}_rough"
      status="ok"
      if ! python3 obw_platform/auto_tuner_dual_fast_pack.py \
        --cfg "$cfg_path" \
        --npz "$NPZ" \
        --symbol "$symbol" \
        --plan "$PLAN" \
        --prefix "$prefix" \
        --jobs "$JOBS" \
        --min-trades "$MIN_TRADES" \
        --score-mode mtm \
        --max-seconds "$MAX_SECONDS_PER_SYMBOL" \
        --debug > "$log" 2>&1; then
        status="failed"
      fi
      summary_path="$(find "_reports/_auto_tuner_dual_fast_pack/$PLAN_STEM" -maxdepth 1 -type d -name "${prefix}_*" -printf '%T@ %p\n' 2>/dev/null | sort -nr | awk 'NR==1{print $2 "/tuner_summary.json"}')"
      printf '| `%s` | %s | %s | %s | `%s` | `%s` |\n' \
        "$symbol" "$score" "$slip" "$status" "$summary_path" "${log#$ROOT/}" >> "$SUMMARY"
    done
  fi

  cp "$SUMMARY" "$run_dir/latest_v21_shortlist_rough_tune.md"
  sleep "$SLEEP_SEC"
done
