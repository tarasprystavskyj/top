#!/usr/bin/env python3
from __future__ import annotations

"""
dex_platform/data_collectors/check_evm_rpc_pool_v1.py

Verify that:
  - RPC is connected.
  - chain_id matches expectation.
  - pool contract has bytecode on that chain.
  - basic CL-pool view calls are attempted.

Use before collecting logs from a new chain/pool.

Example:
  export BSC_RPC_URL="https://bsc-dataseed.binance.org/"
  python3 dex_platform/data_collectors/check_evm_rpc_pool_v1.py \
    --rpc-env BSC_RPC_URL \
    --expected-chain-id 56 \
    --pool 0xe1acb466421ed24dd8bd381d1205bad0ad43ca9c
"""

import argparse
import json
import os
import sys
from typing import Any, Dict

from web3 import Web3


POOL_VIEW_ABI = [
    {"type": "function", "name": "token0", "stateMutability": "view", "inputs": [], "outputs": [{"type": "address"}]},
    {"type": "function", "name": "token1", "stateMutability": "view", "inputs": [], "outputs": [{"type": "address"}]},
    {"type": "function", "name": "tickSpacing", "stateMutability": "view", "inputs": [], "outputs": [{"type": "int24"}]},
    {"type": "function", "name": "fee", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint24"}]},
    {"type": "function", "name": "liquidity", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint128"}]},
]


def call_best_effort(contract, fn_name: str) -> Any:
    try:
        return getattr(contract.functions, fn_name)().call()
    except Exception as e:
        return {"error": str(e)[:500]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", required=True)
    ap.add_argument("--rpc-url", default="")
    ap.add_argument("--rpc-env", default="BASE_RPC_URL")
    ap.add_argument("--expected-chain-id", type=int, default=0)
    args = ap.parse_args()

    rpc_url = args.rpc_url or os.getenv(args.rpc_env, "")
    if not rpc_url:
        raise SystemExit(f"{args.rpc_env} is not set. Refusing to fall back to another chain.")

    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        raise SystemExit(f"RPC not connected: env={args.rpc_env}")

    chain_id = int(w3.eth.chain_id)
    latest = int(w3.eth.block_number)
    pool = w3.to_checksum_address(args.pool)
    code = w3.eth.get_code(pool)
    code_len = len(code)

    result: Dict[str, Any] = {
        "rpc_env": args.rpc_env,
        "rpc_redacted": rpc_url.split("?")[0],
        "chain_id": chain_id,
        "expected_chain_id": args.expected_chain_id or None,
        "latest_block": latest,
        "pool": args.pool.lower(),
        "pool_code_bytes": code_len,
        "pool_has_code": code_len > 0,
    }

    if args.expected_chain_id and chain_id != args.expected_chain_id:
        result["fatal"] = f"wrong chain_id: got {chain_id}, expected {args.expected_chain_id}"
        print(json.dumps(result, indent=2, ensure_ascii=False))
        raise SystemExit(2)

    if code_len <= 0:
        result["fatal"] = "pool has no bytecode on this chain"
        print(json.dumps(result, indent=2, ensure_ascii=False))
        raise SystemExit(3)

    c = w3.eth.contract(address=pool, abi=POOL_VIEW_ABI)
    result["pool_calls"] = {
        "token0": call_best_effort(c, "token0"),
        "token1": call_best_effort(c, "token1"),
        "tickSpacing": call_best_effort(c, "tickSpacing"),
        "fee": call_best_effort(c, "fee"),
        "liquidity": str(call_best_effort(c, "liquidity")),
    }

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
