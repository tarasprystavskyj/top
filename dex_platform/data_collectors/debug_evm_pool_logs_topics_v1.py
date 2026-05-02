#!/usr/bin/env python3
from __future__ import annotations

"""
dex_platform/data_collectors/debug_evm_pool_logs_topics_v1.py

Fetch raw logs from a pool address without topic filter and count topic0.

Use when event collector returns 0 rows:
  - wrong chain/RPC
  - wrong pool address
  - incompatible event ABI
  - no activity in time window

Example:
  export BSC_RPC_URL="https://bsc-dataseed.binance.org/"
  python3 dex_platform/data_collectors/debug_evm_pool_logs_topics_v1.py \
    --rpc-env BSC_RPC_URL \
    --pool 0xe1acb466421ed24dd8bd381d1205bad0ad43ca9c \
    --time-from 2026-05-01T00:00:00Z \
    --time-to 2026-05-02T00:00:00Z \
    --out-csv DEX_DATA/uniswap_v3_bsc/QUG_USDT_001_2026_05_01/topic_counts.csv
"""

import argparse
import datetime as dt
import os
import sys
import time
from pathlib import Path
from collections import Counter
from typing import Dict, Any, List

import pandas as pd
from web3 import Web3


KNOWN_TOPICS = {
    "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67": "UniswapV3/CL Swap",
    "0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde": "UniswapV3/CL Mint",
    "0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c": "UniswapV3/CL Burn",
    "0x70935338e69775456b98a4980e22ec1a8025f8f6c32bd0025865cd4511775f44": "UniswapV3/CL Collect",
}


def iso_to_epoch_s(value: str) -> int:
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    x = dt.datetime.fromisoformat(s)
    if x.tzinfo is None:
        x = x.replace(tzinfo=dt.timezone.utc)
    else:
        x = x.astimezone(dt.timezone.utc)
    return int(x.timestamp())


def block_ts(w3: Web3, block_number: int) -> int:
    return int(w3.eth.get_block(int(block_number))["timestamp"])


def find_block_by_timestamp(w3: Web3, target_ts: int) -> int:
    lo = 0
    hi = int(w3.eth.block_number)
    while lo < hi:
        mid = (lo + hi) // 2
        ts = block_ts(w3, mid)
        if ts < target_ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", required=True)
    ap.add_argument("--rpc-url", default="")
    ap.add_argument("--rpc-env", default="BASE_RPC_URL")
    ap.add_argument("--time-from", required=True)
    ap.add_argument("--time-to", required=True)
    ap.add_argument("--out-csv", required=True)
    ap.add_argument("--chunk-size", type=int, default=2000)
    ap.add_argument("--sleep-s", type=float, default=0.2)
    args = ap.parse_args()

    rpc_url = args.rpc_url or os.getenv(args.rpc_env, "")
    if not rpc_url:
        raise SystemExit(f"{args.rpc_env} is not set")

    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 60}))
    if not w3.is_connected():
        raise SystemExit("RPC not connected")

    pool = w3.to_checksum_address(args.pool)
    from_block = find_block_by_timestamp(w3, iso_to_epoch_s(args.time_from))
    to_block = find_block_by_timestamp(w3, iso_to_epoch_s(args.time_to))

    counts = Counter()
    first_seen: Dict[str, int] = {}
    last_seen: Dict[str, int] = {}

    start = from_block
    total_logs = 0
    while start <= to_block:
        end = min(to_block, start + args.chunk_size - 1)
        logs = w3.eth.get_logs({"address": pool, "fromBlock": start, "toBlock": end})
        total_logs += len(logs)

        for log in logs:
            if log["topics"]:
                t0 = log["topics"][0].hex().lower()
            else:
                t0 = ""
            counts[t0] += 1
            first_seen.setdefault(t0, int(log["blockNumber"]))
            last_seen[t0] = int(log["blockNumber"])

        print(f"[chunk] {start}-{end} logs={len(logs)} total={total_logs}", file=sys.stderr)
        start = end + 1
        time.sleep(args.sleep_s)

    rows = []
    for topic, count in counts.most_common():
        rows.append({
            "topic0": topic,
            "known_label": KNOWN_TOPICS.get(topic, ""),
            "count": count,
            "first_block": first_seen.get(topic),
            "last_block": last_seen.get(topic),
        })

    out = Path(args.out_csv)
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(out, index=False)
    print(f"chain_id={w3.eth.chain_id} from_block={from_block} to_block={to_block} total_logs={total_logs}")
    print(pd.DataFrame(rows).to_string(index=False) if rows else "No logs for this address/time window.")


if __name__ == "__main__":
    main()
