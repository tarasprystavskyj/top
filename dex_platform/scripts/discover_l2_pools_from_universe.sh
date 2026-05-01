#!/usr/bin/env bash
set -euo pipefail
UNIVERSE_FILE="${1:-universe_short_top15_1m.txt}"
OUT_DIR="${2:-DEX_DATA/l2_pool_discovery}"
NETWORKS="${3:-base,arbitrum,optimism}"
mkdir -p "${OUT_DIR}"

python3 dex_platform/data_collectors/discover_geckoterminal_l2_pools_v1.py \
  --universe-file "${UNIVERSE_FILE}" \
  --networks "${NETWORKS}" \
  --out-csv "${OUT_DIR}/candidates_$(basename "${UNIVERSE_FILE}" .txt).csv" \
  --out-json "${OUT_DIR}/candidates_$(basename "${UNIVERSE_FILE}" .txt).json" \
  --min-tvl-usd 10000 \
  --min-volume-h24-usd 5000

python3 - <<PY
import pandas as pd
p="${OUT_DIR}/candidates_$(basename "${UNIVERSE_FILE}" .txt).csv"
df=pd.read_csv(p)
cols=["query_symbol","network","dex_name","pool_name","pool_address","tvl_usd","volume_h24_usd","volume_tvl_h24","tx_h24","score"]
print(df[cols].head(30).to_string(index=False) if len(df) else "No candidates")
PY
