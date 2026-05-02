#!/usr/bin/env python3
from __future__ import annotations

"""
dex_platform/data_collectors/check_evm_rpc_pool_v3.py

Full standalone RPC/pool checker with:
  - script version output
  - PoA middleware support
  - chain guard
  - pool bytecode guard
  - best-effort CL pool view calls
"""

import argparse
import json
import os
import sys
from typing import Any, Dict

from web3 import Web3


SCRIPT_VERSION = "check_evm_rpc_pool_v3_2026_05_02_full_file"


POOL_VIEW_ABI = [
    {"type": "function", "name": "token0", "stateMutability": "view", "inputs": [], "outputs": [{"type": "address"}]},
    {"type": "function", "name": "token1", "stateMutability": "view", "inputs": [], "outputs": [{"type": "address"}]},
    {"type": "function", "name": "tickSpacing", "stateMutability": "view", "inputs": [], "outputs": [{"type": "int24"}]},
    {"type": "function", "name": "fee", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint24"}]},
    {"type": "function", "name": "liquidity", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint128"}]},
]


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


def call_best_effort(contract, fn_name: str) -> Any:
    try:
        val = getattr(contract.functions, fn_name)().call()
        return str(val) if isinstance(val, int) and abs(val) > 10**18 else val
    except Exception as e:
        return {"error": str(e)[:500]}


def main() -> None:
    print_version()

    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", required=True)
    ap.add_argument("--rpc-url", default="")
    ap.add_argument("--rpc-env", default="BASE_RPC_URL")
    ap.add_argument("--expected-chain-id", type=int, default=0)
    ap.add_argument("--no-poa", action="store_true")
    args = ap.parse_args()

    rpc_url = args.rpc_url or os.getenv(args.rpc_env, "")
    if not rpc_url:
        raise SystemExit(f"{args.rpc_env} is not set. Refusing to fall back to another chain.")

    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
    middleware_name = inject_poa_middleware(w3, enabled=not args.no_poa)

    if not w3.is_connected():
        raise SystemExit(f"RPC not connected: env={args.rpc_env}")

    chain_id = int(w3.eth.chain_id)
    latest = int(w3.eth.block_number)
    pool = w3.to_checksum_address(args.pool)
    code = w3.eth.get_code(pool)
    code_len = len(code)

    result: Dict[str, Any] = {
        "script_version": SCRIPT_VERSION,
        "rpc_env": args.rpc_env,
        "rpc_redacted": rpc_url.split("?")[0],
        "middleware": middleware_name,
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
        "liquidity": call_best_effort(c, "liquidity"),
    }

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
