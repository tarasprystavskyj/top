#!/usr/bin/env python3
from __future__ import annotations
import argparse, os, subprocess, sys
from pathlib import Path

SUBGRAPH_IDS = {
    "ethereum": "5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV",
    "eth": "5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV",
    "mainnet": "5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV",
    "arbitrum": "HyW7A86UEdYVt5b9Lrw8W2F98yKecerHKutZTRbSCX27",
    "arbitrum-one": "HyW7A86UEdYVt5b9Lrw8W2F98yKecerHKutZTRbSCX27",
    "base": "FUbEPQw1oMghy39fwWBFY5fE6MXPXZQtjncQy2cXdrNS",
    "optimism": "EgnS9YE1avupkvCNj9fHnJxppfEmNNywYJtghqiu2pd9",
    "op": "EgnS9YE1avupkvCNj9fHnJxppfEmNNywYJtghqiu2pd9",
}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--network", required=True, choices=sorted(SUBGRAPH_IDS.keys()))
    ap.add_argument("--pool", required=True)
    ap.add_argument("--time-from", required=True)
    ap.add_argument("--time-to", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--entities", default="swaps,mints,burns,collects")
    ap.add_argument("--max-pages", type=int, default=0)
    ap.add_argument("--sleep-s", type=float, default=0.05)
    ap.add_argument("--collector", default="dex_platform/data_collectors/fetch_uniswap_v3_pool_events_v1.py")
    args = ap.parse_args()

    if not os.getenv("THEGRAPH_API_KEY"):
        raise SystemExit("THEGRAPH_API_KEY is not set.")
    collector = Path(args.collector)
    if not collector.exists():
        raise SystemExit(f"Base collector not found: {collector}")

    cmd = [
        sys.executable, str(collector),
        "--pool", args.pool,
        "--time-from", args.time_from,
        "--time-to", args.time_to,
        "--out-dir", args.out_dir,
        "--subgraph-id", SUBGRAPH_IDS[args.network],
        "--entities", args.entities,
        "--sleep-s", str(args.sleep_s),
    ]
    if args.max_pages > 0:
        cmd.extend(["--max-pages", str(args.max_pages)])
    print("[run]", " ".join(cmd))
    subprocess.check_call(cmd)

if __name__ == "__main__":
    main()
