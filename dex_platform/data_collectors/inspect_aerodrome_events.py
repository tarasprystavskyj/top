#!/usr/bin/env python3
from __future__ import annotations
import sys
from pathlib import Path
import pandas as pd

def main():
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python3 dex_platform/data_collectors/inspect_aerodrome_events.py <events_all.csv|parquet>")
    p = Path(sys.argv[1])
    if not p.exists():
        raise SystemExit(f"not found: {p}")

    df = pd.read_parquet(p) if p.suffix.lower() == ".parquet" else pd.read_csv(p)
    print("file:", p)
    print("rows:", len(df))
    if df.empty:
        return
    if "event_type" in df.columns:
        print("\nevent counts:")
        print(df["event_type"].value_counts(dropna=False).to_string())
    if "timestamp" in df.columns:
        ts = pd.to_numeric(df["timestamp"], errors="coerce")
        print("\nfrom:", pd.to_datetime(ts.min(), unit="s", utc=True))
        print("to:  ", pd.to_datetime(ts.max(), unit="s", utc=True))
    for col in ["amount0","amount1","amount","liquidity","tick","sqrtPriceX96"]:
        if col in df.columns:
            s = pd.to_numeric(df[col], errors="coerce")
            print(f"\n{col}: non-null={int(s.notna().sum())} min={s.min()} max={s.max()}")

if __name__ == "__main__":
    main()
