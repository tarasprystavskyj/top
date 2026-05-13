#!/usr/bin/env bash
set -euo pipefail

ROOT="/var/www/vps2.happyuser.info/top/top_1"
LANE="$ROOT/obw_platform/meta_strategies/akela_meta_short"
RANK_CSV="${AKELA_ROUGH_TUNE_RANK_CSV:-$ROOT/_reports/akela_meta_short/v21_midcaps_rank/v21_majors_rank.csv}"
NPZ="${AKELA_ROUGH_TUNE_NPZ:-$ROOT/_reports/akela_meta_short/v21_midcaps_rank/majors_bingx_5m_1500b.npz}"
BASE_CFG="${AKELA_ROUGH_TUNE_BASE_CFG:-$ROOT/obw_platform/meta_strategies/akela_meta_short/live_freedommoney/V21_freedommoney_bingx_live_min2p2.yaml}"
PLAN="${AKELA_ROUGH_TUNE_PLAN:-$ROOT/obw_platform/tuner_plans/tuner_plan_V21_live_candidates_1m_1y.py}"
OUT_ROOT="${AKELA_ROUGH_TUNE_OUT_ROOT:-$ROOT/_reports/akela_meta_short/v21_shortlist_rough_tune}"
SUMMARY="$LANE/reports/latest_v21_shortlist_rough_tune.md"

MAX_SYMBOLS="${AKELA_ROUGH_TUNE_MAX_SYMBOLS:-10}"
MAX_SECONDS_PER_SYMBOL="${AKELA_ROUGH_TUNE_MAX_SECONDS_PER_SYMBOL:-1800}"
JOBS="${AKELA_ROUGH_TUNE_JOBS:-1}"
MIN_TRADES="${AKELA_ROUGH_TUNE_MIN_TRADES:-50}"
SLEEP_SEC="${AKELA_ROUGH_TUNE_SLEEP_SEC:-21600}"

mkdir -p "$OUT_ROOT" "$LANE/reports"
cd "$ROOT"

safe_slug() {
  printf '%s' "$1" | tr '/:. ' '____' | tr -cd 'A-Za-z0-9_-'
}

while true; do
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  run_dir="$OUT_ROOT/$stamp"
  mkdir -p "$run_dir/cfg" "$run_dir/logs"

  python3 - "$RANK_CSV" "$BASE_CFG" "$run_dir" "$MAX_SYMBOLS" <<'PY'
import sys
from pathlib import Path

import pandas as pd
import yaml

rank_csv = Path(sys.argv[1])
base_cfg = Path(sys.argv[2])
run_dir = Path(sys.argv[3])
max_symbols = int(sys.argv[4])

df = pd.read_csv(rank_csv)
scenario_order = {"passive_spread_p95": 0, "passive_spread_p50": 1, "cfg_static": 2}
df["scenario_rank"] = df["scenario"].map(scenario_order).fillna(9)
df = df[
    (df["scenario"] == "passive_spread_p95")
    & (df["margin_call_events_total"].fillna(999).astype(float) == 0)
].copy()
df["tail_abs"] = df["terminal_unrealized_to_realized_ratio"].fillna(0).astype(float).abs()
df = df[df["tail_abs"] <= 2.0].copy()
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
      summary_path="$(python3 - "$log" <<'PY'
import json, sys
text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
start = text.rfind("{")
if start >= 0:
    try:
        obj = json.loads(text[start:])
        print(obj.get("session_dir", "") + "/tuner_summary.json")
    except Exception:
        print("")
PY
)"
      printf '| `%s` | %s | %s | %s | `%s` | `%s` |\n' \
        "$symbol" "$score" "$slip" "$status" "$summary_path" "${log#$ROOT/}" >> "$SUMMARY"
    done
  fi

  cp "$SUMMARY" "$run_dir/latest_v21_shortlist_rough_tune.md"
  sleep "$SLEEP_SEC"
done
