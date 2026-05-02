#!/usr/bin/env python3
from __future__ import annotations

"""
dex_platform/data_collectors/debug_evm_pool_logs_topics_v3.py

Full standalone raw-topic debugger with:
  - script version output
  - PoA middleware support
  - chain guard
  - raw log topic0 counts without ABI filter
"""

import argparse
import datetime as dt
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict

import pandas as pd
from web3 import Web3


SCRIPT_VERSION = "debug_evm_pool_logs_topics_v3_2026_05_02_full_file"


KNOWN_TOPICS = {
    "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67": "UniswapV3/CL Swap",
    "0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde": "UniswapV3/CL Mint",
    "0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c": "UniswapV3/CL Burn",
    "0x70935338e69775456b98a4980e22ec1a8025f8f6c32bd0025865cd4511775f44": "UniswapV3/CL Collect",
}


def print_version() -> None:
    print(f"[script_version] {__file__} SCRIPT_VERSION={SCRIPT_VERSION}", file=sys.stderr)


def inject_poa_middleware(w3: Web3, *, enabled: bool = True) -> str:
    if not enabled:
        print("[web3] PoA middleware disabled by CLI", file=sys.stderr)
        return "disabled"

    errors = []
    try:
        from web3.middleware import geth_poa_middleware
        w3.middleware_onion.inject(geth_poa_middleware, layer=0)
        print("[web3] injected geth_poa_middleware", file=sys.stderr)
        return "geth_poa_middleware"
    except Exception as e:
        errors.append(f"geth_poa_middleware={e}")

    try:
        from web3.middleware.proof_of_authority import ExtraDataToPOAMiddleware
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        print("[web3] injected proof_of_authority.ExtraDataToPOAMiddleware", file=sys.stderr)
        return "proof_of_authority.ExtraDataToPOAMiddleware"
    except Exception as e:
        errors.append(f"proof_of_authority.ExtraDataToPOAMiddleware={e}")

    try:
        from web3.middleware import ExtraDataToPOAMiddleware
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        print("[web3] injected ExtraDataToPOAMiddleware", file=sys.stderr)
        return "ExtraDataToPOAMiddleware"
    except Exception as e:
        errors.append(f"ExtraDataToPOAMiddleware={e}")

    raise RuntimeError("Could not inject PoA middleware: " + " | ".join(errors))


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
    print_version()

    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", required=True)
    ap.add_argument("--rpc-url", default="")
    ap.add_argument("--rpc-env", default="BASE_RPC_URL")
    ap.add_argument("--expected-chain-id", type=int, default=0)
    ap.add_argument("--time-from", required=True)
    ap.add_argument("--time-to", required=True)
    ap.add_argument("--out-csv", required=True)
    ap.add_argument("--chunk-size", type=int, default=2000)
    ap.add_argument("--sleep-s", type=float, default=0.2)
    ap.add_argument("--no-poa", action="store_true")
    args = ap.parse_args()

    rpc_url = args.rpc_url or os.getenv(args.rpc_env, "")
    if not rpc_url:
        raise SystemExit(f"{args.rpc_env} is not set")

    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 60}))
    middleware_name = inject_poa_middleware(w3, enabled=not args.no_poa)

    if not w3.is_connected():
        raise SystemExit("RPC not connected")

    chain_id = int(w3.eth.chain_id)
    print(f"[rpc] env={args.rpc_env} chain_id={chain_id} middleware={middleware_name}", file=sys.stderr)

    if args.expected_chain_id and chain_id != args.expected_chain_id:
        raise SystemExit(f"wrong chain_id: got {chain_id}, expected {args.expected_chain_id}")

    pool = w3.to_checksum_address(args.pool)
    code = w3.eth.get_code(pool)
    if len(code) <= 0:
        raise SystemExit(f"pool has no code on chain_id={chain_id}: {args.pool}")

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
            t0 = log["topics"][0].hex().lower() if log["topics"] else ""
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

    print(f"script_version={SCRIPT_VERSION} chain_id={chain_id} middleware={middleware_name} from_block={from_block} to_block={to_block} total_logs={total_logs}")
    print(pd.DataFrame(rows).to_string(index=False) if rows else "No logs for this address/time window.")


if __name__ == "__main__":
    main()
