#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from web3 import Web3


def inject_poa(w3):
    errors = []
    try:
        from web3.middleware import geth_poa_middleware
        w3.middleware_onion.inject(geth_poa_middleware, layer=0)
        print("[test] injected geth_poa_middleware")
        return
    except Exception as e:
        errors.append(str(e))

    try:
        from web3.middleware.proof_of_authority import ExtraDataToPOAMiddleware
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        print("[test] injected proof_of_authority.ExtraDataToPOAMiddleware")
        return
    except Exception as e:
        errors.append(str(e))

    try:
        from web3.middleware import ExtraDataToPOAMiddleware
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        print("[test] injected ExtraDataToPOAMiddleware")
        return
    except Exception as e:
        errors.append(str(e))

    raise RuntimeError("No PoA middleware import worked: " + " | ".join(errors))


rpc = os.getenv("BSC_RPC_URL")
if not rpc:
    raise SystemExit("BSC_RPC_URL is not set")

w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 30}))
inject_poa(w3)

print("connected:", w3.is_connected())
print("chain_id:", w3.eth.chain_id)
print("latest:", w3.eth.block_number)
b = w3.eth.get_block(w3.eth.block_number)
print("latest_ts:", b["timestamp"])
print("extraData_len:", len(b.get("extraData", b"")))
