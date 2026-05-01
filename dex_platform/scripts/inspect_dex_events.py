#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path
import pandas as pd

def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python3 scripts/inspect_dex_events.py <events_all.csv|parquet>")

    p = Path(sys.argv[1])
    if not p.exists():
        raise SystemExit(f"File not found: {p}")

    if p.suffix.lower() == ".parquet":
        df = pd.read_parquet(p)
    else:
        df = pd.read_csv(p)

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

    for col in ["amount_usd", "gas_used", "gas_price_wei"]:
        if col in df.columns:
            s = pd.to_numeric(df[col], errors="coerce")
            print(f"\n{col}:")
            print("  non-null:", int(s.notna().sum()))
            print("  sum:", float(s.sum(skipna=True)))
            print("  mean:", float(s.mean(skipna=True)) if s.notna().any() else 0.0)

if __name__ == "__main__":
    main()
