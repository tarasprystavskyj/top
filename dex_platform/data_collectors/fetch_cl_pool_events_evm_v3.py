#!/usr/bin/env python3
from __future__ import annotations

"""
dex_platform/data_collectors/fetch_cl_pool_events_evm_v3.py

Generic EVM concentrated-liquidity pool event collector for:
  - Aerodrome / Velodrome Slipstream-like pools
  - Uniswap V3-like pools
  - PancakeSwap V3-like pools

Why this file exists:
  Previous patch-based flow was fragile. This is a full standalone file with:
    - explicit SCRIPT_VERSION output
    - BSC/PoA middleware injection
    - chain guard
    - pool bytecode guard
    - parquet-safe huge integer conversion
    - retry/backoff
    - adaptive get_logs range splitting

Primary current targets:
  BSC QUG/USDT:
    0xe1acb466421ed24dd8bd381d1205bad0ad43ca9c

  Base CHECK/USDC Aerodrome:
    0x5a7b4970b2610aee4776a6944d9f2171ee6060b0

Install:
  python3 -m pip install web3 pandas pyarrow

Example BSC:
  export BSC_RPC_URL="https://bsc-dataseed.binance.org/"

  python3 dex_platform/data_collectors/fetch_cl_pool_events_evm_v3.py \
    --pool 0xe1acb466421ed24dd8bd381d1205bad0ad43ca9c \
    --rpc-env BSC_RPC_URL \
    --expected-chain-id 56 \
    --time-from 2026-05-01T00:00:00Z \
    --time-to   2026-05-02T00:00:00Z \
    --out-dir DEX_DATA/uniswap_v3_bsc/QUG_USDT_001_2026_05_01_v3 \
    --events Swap,Mint,Burn,Collect
"""

import argparse
import datetime as dt
import json
import math
import os
import random
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pandas as pd
from eth_utils import event_abi_to_log_topic
from web3 import Web3
from web3._utils.events import get_event_data


SCRIPT_VERSION = "fetch_cl_pool_events_evm_v3_2026_05_02_full_file"


CLPOOL_EVENT_ABIS: List[Dict[str, Any]] = [
    {
        "anonymous": False,
        "type": "event",
        "name": "Initialize",
        "inputs": [
            {"indexed": False, "internalType": "uint160", "name": "sqrtPriceX96", "type": "uint160"},
            {"indexed": False, "internalType": "int24", "name": "tick", "type": "int24"},
        ],
    },
    {
        "anonymous": False,
        "type": "event",
        "name": "Mint",
        "inputs": [
            {"indexed": False, "internalType": "address", "name": "sender", "type": "address"},
            {"indexed": True, "internalType": "address", "name": "owner", "type": "address"},
            {"indexed": True, "internalType": "int24", "name": "tickLower", "type": "int24"},
            {"indexed": True, "internalType": "int24", "name": "tickUpper", "type": "int24"},
            {"indexed": False, "internalType": "uint128", "name": "amount", "type": "uint128"},
            {"indexed": False, "internalType": "uint256", "name": "amount0", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "amount1", "type": "uint256"},
        ],
    },
    {
        "anonymous": False,
        "type": "event",
        "name": "Burn",
        "inputs": [
            {"indexed": True, "internalType": "address", "name": "owner", "type": "address"},
            {"indexed": True, "internalType": "int24", "name": "tickLower", "type": "int24"},
            {"indexed": True, "internalType": "int24", "name": "tickUpper", "type": "int24"},
            {"indexed": False, "internalType": "uint128", "name": "amount", "type": "uint128"},
            {"indexed": False, "internalType": "uint256", "name": "amount0", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "amount1", "type": "uint256"},
        ],
    },
    {
        "anonymous": False,
        "type": "event",
        "name": "Collect",
        "inputs": [
            {"indexed": True, "internalType": "address", "name": "owner", "type": "address"},
            {"indexed": False, "internalType": "address", "name": "recipient", "type": "address"},
            {"indexed": True, "internalType": "int24", "name": "tickLower", "type": "int24"},
            {"indexed": True, "internalType": "int24", "name": "tickUpper", "type": "int24"},
            {"indexed": False, "internalType": "uint128", "name": "amount0", "type": "uint128"},
            {"indexed": False, "internalType": "uint128", "name": "amount1", "type": "uint128"},
        ],
    },
    {
        "anonymous": False,
        "type": "event",
        "name": "CollectFees",
        "inputs": [
            {"indexed": True, "internalType": "address", "name": "recipient", "type": "address"},
            {"indexed": False, "internalType": "uint128", "name": "amount0", "type": "uint128"},
            {"indexed": False, "internalType": "uint128", "name": "amount1", "type": "uint128"},
        ],
    },
    {
        "anonymous": False,
        "type": "event",
        "name": "Swap",
        "inputs": [
            {"indexed": True, "internalType": "address", "name": "sender", "type": "address"},
            {"indexed": True, "internalType": "address", "name": "recipient", "type": "address"},
            {"indexed": False, "internalType": "int256", "name": "amount0", "type": "int256"},
            {"indexed": False, "internalType": "int256", "name": "amount1", "type": "int256"},
            {"indexed": False, "internalType": "uint160", "name": "sqrtPriceX96", "type": "uint160"},
            {"indexed": False, "internalType": "uint128", "name": "liquidity", "type": "uint128"},
            {"indexed": False, "internalType": "int24", "name": "tick", "type": "int24"},
        ],
    },
    {
        "anonymous": False,
        "type": "event",
        "name": "Flash",
        "inputs": [
            {"indexed": True, "internalType": "address", "name": "sender", "type": "address"},
            {"indexed": True, "internalType": "address", "name": "recipient", "type": "address"},
            {"indexed": False, "internalType": "uint256", "name": "amount0", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "amount1", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "paid0", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "paid1", "type": "uint256"},
        ],
    },
]


POOL_VIEW_ABI = [
    {"type": "function", "name": "token0", "stateMutability": "view", "inputs": [], "outputs": [{"type": "address"}]},
    {"type": "function", "name": "token1", "stateMutability": "view", "inputs": [], "outputs": [{"type": "address"}]},
    {"type": "function", "name": "tickSpacing", "stateMutability": "view", "inputs": [], "outputs": [{"type": "int24"}]},
    {"type": "function", "name": "fee", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint24"}]},
    {"type": "function", "name": "liquidity", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint128"}]},
]


BIG_INT_COLUMNS = {
    "amount", "amount0", "amount1", "paid0", "paid1", "sqrtPriceX96", "liquidity", "data"
}
SAFE_NUMERIC_COLUMNS = {
    "blockNumber", "transactionIndex", "logIndex", "timestamp", "tick", "tickLower", "tickUpper"
}


def print_version() -> None:
    print(f"[script_version] {__file__} SCRIPT_VERSION={SCRIPT_VERSION}", file=sys.stderr)


def inject_poa_middleware(w3: Web3, *, enabled: bool = True) -> str:
    if not enabled:
        print("[web3] PoA middleware disabled by CLI", file=sys.stderr)
        return "disabled"

    errors: List[str] = []

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

    msg = "Could not inject PoA middleware: " + " | ".join(errors)
    print("[web3][ERROR]", msg, file=sys.stderr)
    raise RuntimeError(msg)


def json_safe(x: Any) -> Any:
    if isinstance(x, (bytes, bytearray)):
        return "0x" + x.hex()
    try:
        from hexbytes import HexBytes
        if isinstance(x, HexBytes):
            return x.hex()
    except Exception:
        pass
    if isinstance(x, dict):
        return {str(k): json_safe(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [json_safe(v) for v in x]
    return x


def iso_to_epoch_s(value: str) -> int:
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    x = dt.datetime.fromisoformat(s)
    if x.tzinfo is None:
        x = x.replace(tzinfo=dt.timezone.utc)
    else:
        x = x.astimezone(dt.timezone.utc)
    return int(x.timestamp())


def retry_call(
    fn: Callable[[], Any],
    *,
    label: str,
    attempts: int = 6,
    base_sleep: float = 1.0,
    max_sleep: float = 45.0,
) -> Any:
    last_exc: Optional[Exception] = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            wait = min(max_sleep, base_sleep * (2 ** i)) + random.random() * 0.25
            print(f"[WARN] {label} failed attempt={i+1}/{attempts}: {e}; sleep {wait:.2f}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"{label} failed after {attempts} attempts: {last_exc}") from last_exc


def make_w3(rpc_url: str, *, inject_poa: bool, timeout: int = 90) -> tuple[Web3, str]:
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": timeout}))
    middleware_name = inject_poa_middleware(w3, enabled=inject_poa)
    if not w3.is_connected():
        raise SystemExit(f"RPC not connected: {rpc_url.split('?')[0]}")
    return w3, middleware_name


def block_ts(w3: Web3, block_number: int, *, attempts: int = 6) -> int:
    block = retry_call(
        lambda: w3.eth.get_block(int(block_number)),
        label=f"get_block({block_number})",
        attempts=attempts,
    )
    return int(block["timestamp"])


def find_block_by_timestamp(w3: Web3, target_ts: int, *, high: Optional[int] = None, attempts: int = 6) -> int:
    if high is None:
        high = int(retry_call(lambda: w3.eth.block_number, label="eth.block_number", attempts=attempts))

    lo = 0
    hi = int(high)

    # Avoid forcing block 0 on PoA chains unless needed.
    while lo < hi:
        mid = (lo + hi) // 2
        ts = block_ts(w3, mid, attempts=attempts)
        if ts < target_ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


def make_topic_map() -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for abi in CLPOOL_EVENT_ABIS:
        topic = event_abi_to_log_topic(abi).hex()
        if not topic.startswith("0x"):
            topic = "0x" + topic
        out[topic.lower()] = abi
    return out


def raw_log_to_json(log: Dict[str, Any]) -> Dict[str, Any]:
    data_val = log.get("data")
    if hasattr(data_val, "hex"):
        data = data_val.hex()
    else:
        data = str(data_val)
    if not data.startswith("0x"):
        data = "0x" + data

    return {
        "address": str(log.get("address", "")).lower(),
        "blockNumber": int(log["blockNumber"]),
        "transactionHash": log["transactionHash"].hex(),
        "transactionIndex": int(log["transactionIndex"]),
        "logIndex": int(log["logIndex"]),
        "removed": bool(log.get("removed", False)),
        "topics": [t.hex() if str(t.hex()).startswith("0x") else "0x" + t.hex() for t in log["topics"]],
        "data": data,
    }


def decode_log(w3: Web3, log: Dict[str, Any], topic_map: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    raw = raw_log_to_json(log)
    topic0 = raw["topics"][0].lower() if raw["topics"] else ""
    abi = topic_map.get(topic0)
    if not abi:
        raw["event_type"] = "UNKNOWN"
        raw["decode_ok"] = False
        raw["decode_error"] = "unknown topic0"
        return raw

    raw["event_type"] = abi["name"]
    try:
        decoded = get_event_data(w3.codec, abi, log)
        raw["decode_ok"] = True
        args = dict(decoded.get("args", {}))
        for k, v in args.items():
            val = json_safe(v)
            raw[k] = str(val) if k in BIG_INT_COLUMNS else val
    except Exception as e:
        raw["decode_ok"] = False
        raw["decode_error"] = str(e)

    return raw


def parquet_safe_df(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in out.columns:
        if col in BIG_INT_COLUMNS:
            out[col] = out[col].map(lambda x: None if pd.isna(x) else str(x))
        elif col == "topics":
            out[col] = out[col].map(lambda x: json.dumps(x, ensure_ascii=False) if not isinstance(x, str) else x)
        elif col in SAFE_NUMERIC_COLUMNS:
            out[col] = pd.to_numeric(out[col], errors="coerce")
        elif col.endswith("_error") or col in {
            "transactionHash", "address", "owner", "sender", "recipient",
            "event_type", "datetime_utc", "decode_error",
        }:
            out[col] = out[col].astype("string")
    return out


def save_table(df: pd.DataFrame, base: Path, *, write_parquet: bool) -> Dict[str, str]:
    out: Dict[str, str] = {}
    csv_path = base.with_suffix(".csv")
    df.to_csv(csv_path, index=False)
    out["csv"] = str(csv_path)

    if write_parquet:
        try:
            pq_path = base.with_suffix(".parquet")
            parquet_safe_df(df).to_parquet(pq_path, index=False)
            out["parquet"] = str(pq_path)
        except Exception as e:
            out["parquet_error"] = str(e)
    return out


def get_logs_adaptive(
    w3: Web3,
    *,
    address: str,
    topic0: str,
    from_block: int,
    to_block: int,
    min_chunk_size: int,
    attempts: int,
) -> List[Dict[str, Any]]:
    params = {
        "address": address,
        "fromBlock": int(from_block),
        "toBlock": int(to_block),
        "topics": [topic0],
    }
    try:
        return list(retry_call(
            lambda: w3.eth.get_logs(params),
            label=f"get_logs({from_block}-{to_block})",
            attempts=attempts,
        ))
    except Exception as e:
        size = int(to_block) - int(from_block) + 1
        if size <= int(min_chunk_size):
            raise RuntimeError(f"get_logs failed at min range {from_block}-{to_block}: {e}") from e
        mid = (int(from_block) + int(to_block)) // 2
        print(f"[WARN] splitting get_logs range {from_block}-{to_block} -> {from_block}-{mid}, {mid+1}-{to_block}", file=sys.stderr)
        return (
            get_logs_adaptive(
                w3, address=address, topic0=topic0, from_block=from_block,
                to_block=mid, min_chunk_size=min_chunk_size, attempts=attempts
            )
            + get_logs_adaptive(
                w3, address=address, topic0=topic0, from_block=mid + 1,
                to_block=to_block, min_chunk_size=min_chunk_size, attempts=attempts
            )
        )


def call_best_effort(contract, fn_name: str) -> Any:
    try:
        return json_safe(getattr(contract.functions, fn_name)().call())
    except Exception as e:
        return {"error": str(e)[:500]}


def get_pool_metadata(w3: Web3, pool: str) -> Dict[str, Any]:
    pool_cs = w3.to_checksum_address(pool)
    code = w3.eth.get_code(pool_cs)
    c = w3.eth.contract(address=pool_cs, abi=POOL_VIEW_ABI)

    out: Dict[str, Any] = {
        "pool": pool.lower(),
        "pool_code_bytes": len(code),
        "pool_has_code": len(code) > 0,
        "token0": call_best_effort(c, "token0"),
        "token1": call_best_effort(c, "token1"),
        "tickSpacing": call_best_effort(c, "tickSpacing"),
        "fee": call_best_effort(c, "fee"),
        "liquidity": str(call_best_effort(c, "liquidity")),
    }
    return out


def main() -> None:
    print_version()

    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", required=True)
    ap.add_argument("--rpc-url", default="")
    ap.add_argument("--rpc-env", default="BASE_RPC_URL")
    ap.add_argument("--expected-chain-id", type=int, default=0)
    ap.add_argument("--time-from", default="")
    ap.add_argument("--time-to", default="")
    ap.add_argument("--from-block", type=int, default=0)
    ap.add_argument("--to-block", type=int, default=0)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--chunk-size", type=int, default=2000)
    ap.add_argument("--min-chunk-size", type=int, default=100)
    ap.add_argument("--sleep-s", type=float, default=0.2)
    ap.add_argument("--attempts", type=int, default=6)
    ap.add_argument("--events", "--only-events", dest="events", default="Swap,Mint,Burn,Collect")
    ap.add_argument("--no-parquet", action="store_true")
    ap.add_argument("--no-poa", action="store_true")
    args = ap.parse_args()

    rpc_url = args.rpc_url or os.getenv(args.rpc_env, "")
    if not rpc_url:
        raise SystemExit(f"{args.rpc_env} is not set. Refusing to fall back to another chain.")

    w3, middleware_name = make_w3(rpc_url, inject_poa=not args.no_poa)
    chain_id = int(w3.eth.chain_id)
    latest = int(w3.eth.block_number)
    print(f"[rpc] env={args.rpc_env} chain_id={chain_id} latest={latest} middleware={middleware_name}", file=sys.stderr)

    if args.expected_chain_id and chain_id != args.expected_chain_id:
        raise SystemExit(f"wrong chain_id: got {chain_id}, expected {args.expected_chain_id}")

    pool = w3.to_checksum_address(args.pool)
    metadata = get_pool_metadata(w3, args.pool)
    if not metadata.get("pool_has_code"):
        raise SystemExit(f"pool has no code on chain_id={chain_id}: {args.pool}")

    if args.from_block and args.to_block:
        from_block = int(args.from_block)
        to_block = int(args.to_block)
        from_ts = block_ts(w3, from_block, attempts=args.attempts)
        to_ts = block_ts(w3, to_block, attempts=args.attempts)
    else:
        if not args.time_from or not args.time_to:
            raise SystemExit("Use either --from-block/--to-block or --time-from/--time-to.")
        from_ts = iso_to_epoch_s(args.time_from)
        to_ts = iso_to_epoch_s(args.time_to)
        from_block = find_block_by_timestamp(w3, from_ts, high=latest, attempts=args.attempts)
        to_block = find_block_by_timestamp(w3, to_ts, high=latest, attempts=args.attempts)

    if to_block < from_block:
        raise SystemExit(f"to_block < from_block: {to_block} < {from_block}")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    metadata.update({
        "script_version": SCRIPT_VERSION,
        "rpc_env": args.rpc_env,
        "rpc_redacted": rpc_url.split("?")[0],
        "middleware": middleware_name,
        "chain_id": chain_id,
        "latest_block_at_start": latest,
        "from_block": from_block,
        "to_block": to_block,
        "from_ts": from_ts,
        "to_ts": to_ts,
        "time_from": args.time_from,
        "time_to": args.time_to,
        "chunk_size": args.chunk_size,
        "min_chunk_size": args.min_chunk_size,
    })
    (out_dir / "pool_metadata.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")

    topic_map = make_topic_map()
    wanted = {x.strip() for x in args.events.split(",") if x.strip()}
    wanted_topics: Dict[str, Dict[str, Any]] = {
        topic: abi for topic, abi in topic_map.items() if abi["name"] in wanted
    }
    if not wanted_topics:
        raise SystemExit(f"No matching events selected. Requested={sorted(wanted)}")

    rows: List[Dict[str, Any]] = []
    raw_path = out_dir / "events.raw.jsonl"
    ts_cache: Dict[int, int] = {}

    def get_cached_ts(bn: int) -> int:
        if bn not in ts_cache:
            ts_cache[bn] = block_ts(w3, bn, attempts=args.attempts)
        return ts_cache[bn]

    with raw_path.open("w", encoding="utf-8") as raw_f:
        for event_topic, event_abi in wanted_topics.items():
            ev_name = event_abi["name"]
            print(f"[event] {ev_name} topic={event_topic}", file=sys.stderr)
            start = int(from_block)
            total_ev_logs = 0

            while start <= int(to_block):
                end = min(int(to_block), start + int(args.chunk_size) - 1)
                logs = get_logs_adaptive(
                    w3,
                    address=pool,
                    topic0=event_topic,
                    from_block=start,
                    to_block=end,
                    min_chunk_size=int(args.min_chunk_size),
                    attempts=int(args.attempts),
                )

                for log in logs:
                    raw_json = raw_log_to_json(log)
                    raw_f.write(json.dumps(raw_json, ensure_ascii=False) + "\n")
                    decoded = decode_log(w3, log, topic_map)
                    bn = int(decoded["blockNumber"])
                    decoded["timestamp"] = get_cached_ts(bn)
                    decoded["datetime_utc"] = dt.datetime.fromtimestamp(
                        decoded["timestamp"], tz=dt.timezone.utc
                    ).isoformat()
                    rows.append(decoded)

                total_ev_logs += len(logs)
                print(
                    f"[chunk] event={ev_name} blocks={start}-{end} "
                    f"logs={len(logs)} event_total={total_ev_logs} all_total={len(rows)}",
                    file=sys.stderr,
                )
                start = end + 1
                if args.sleep_s > 0:
                    time.sleep(float(args.sleep_s))

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["blockNumber", "transactionIndex", "logIndex"], kind="stable").reset_index(drop=True)

    files = save_table(df, out_dir / "events_all", write_parquet=not args.no_parquet)

    summary = {
        "script_version": SCRIPT_VERSION,
        "pool": args.pool.lower(),
        "rpc_env": args.rpc_env,
        "chain_id": chain_id,
        "middleware": middleware_name,
        "from_block": int(from_block),
        "to_block": int(to_block),
        "rows": int(len(df)),
        "event_counts": df["event_type"].value_counts().to_dict() if not df.empty and "event_type" in df.columns else {},
        "decode_ok_counts": df["decode_ok"].value_counts(dropna=False).to_dict() if not df.empty and "decode_ok" in df.columns else {},
        "files": {
            **files,
            "raw_jsonl": str(raw_path),
            "metadata": str(out_dir / "pool_metadata.json"),
        },
        "warning": "Raw on-chain events collected. Fee accounting still requires liquidity/tick accounting and position simulation.",
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
