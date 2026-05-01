#!/usr/bin/env python3
from __future__ import annotations

"""
dex_platform/data_collectors/fetch_aerodrome_slipstream_events_v1.py

Fetch Aerodrome Slipstream / Velodrome CL-style pool events from Base RPC.

Why:
  GeckoTerminal OHLCV is enough for scouting price path.
  It is NOT enough for LP fee accounting.

This collector fetches raw on-chain logs for a single concentrated-liquidity pool:
  - Initialize
  - Mint
  - Burn
  - Collect
  - CollectFees
  - Swap
  - Flash
  - IncreaseObservationCardinalityNext
  - SetFeeProtocol

Primary target:
  CHECK/USDC Aerodrome Slipstream 2%
  0x5a7b4970b2610aee4776a6944d9f2171ee6060b0

Requirements:
  python3 -m pip install web3 pandas pyarrow

RPC:
  export BASE_RPC_URL="https://mainnet.base.org"
  # Better: use paid/own RPC if public endpoint rate-limits.

Example:
  python3 dex_platform/data_collectors/fetch_aerodrome_slipstream_events_v1.py \
    --pool 0x5a7b4970b2610aee4776a6944d9f2171ee6060b0 \
    --time-from 2026-02-01T00:00:00Z \
    --time-to   2026-05-01T00:00:00Z \
    --out-dir DEX_DATA/aerodrome_slipstream/base_CHECK_USDC_2PCT_2026_02_05 \
    --chunk-size 5000

Notes:
  - Timestamp -> block is found by binary search.
  - Logs are fetched by block ranges.
  - If the RPC rate-limits, lower --chunk-size and increase --sleep-s.
  - Decoding is best-effort. Raw logs are saved regardless.
"""

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
from web3 import Web3
from web3._utils.events import get_event_data
from eth_utils import event_abi_to_log_topic


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
    {
        "anonymous": False,
        "type": "event",
        "name": "IncreaseObservationCardinalityNext",
        "inputs": [
            {"indexed": False, "internalType": "uint16", "name": "observationCardinalityNextOld", "type": "uint16"},
            {"indexed": False, "internalType": "uint16", "name": "observationCardinalityNextNew", "type": "uint16"},
        ],
    },
    {
        "anonymous": False,
        "type": "event",
        "name": "SetFeeProtocol",
        "inputs": [
            {"indexed": False, "internalType": "uint8", "name": "feeProtocol0Old", "type": "uint8"},
            {"indexed": False, "internalType": "uint8", "name": "feeProtocol1Old", "type": "uint8"},
            {"indexed": False, "internalType": "uint8", "name": "feeProtocol0New", "type": "uint8"},
            {"indexed": False, "internalType": "uint8", "name": "feeProtocol1New", "type": "uint8"},
        ],
    },
]


POOL_VIEW_ABI = [
    {"type": "function", "name": "token0", "stateMutability": "view", "inputs": [], "outputs": [{"type": "address"}]},
    {"type": "function", "name": "token1", "stateMutability": "view", "inputs": [], "outputs": [{"type": "address"}]},
    {"type": "function", "name": "tickSpacing", "stateMutability": "view", "inputs": [], "outputs": [{"type": "int24"}]},
    {"type": "function", "name": "fee", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint24"}]},
    {"type": "function", "name": "liquidity", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint128"}]},
    {
        "type": "function",
        "name": "slot0",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [
            {"type": "uint160", "name": "sqrtPriceX96"},
            {"type": "int24", "name": "tick"},
            {"type": "uint16", "name": "observationIndex"},
            {"type": "uint16", "name": "observationCardinality"},
            {"type": "uint16", "name": "observationCardinalityNext"},
            {"type": "uint8", "name": "feeProtocol"},
            {"type": "bool", "name": "unlocked"},
        ],
    },
]


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


def block_ts(w3: Web3, block_number: int) -> int:
    return int(w3.eth.get_block(int(block_number))["timestamp"])


def find_block_by_timestamp(w3: Web3, target_ts: int, *, low: int = 0, high: Optional[int] = None) -> int:
    """Find first block with timestamp >= target_ts."""
    if high is None:
        high = int(w3.eth.block_number)

    lo = int(low)
    hi = int(high)

    # Narrow low if chain started much later than 0; keeps binary search safe.
    try:
        if block_ts(w3, lo) >= target_ts:
            return lo
    except Exception:
        lo = 1

    while lo < hi:
        mid = (lo + hi) // 2
        ts = block_ts(w3, mid)
        if ts < target_ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


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
        return {k: json_safe(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [json_safe(v) for v in x]
    try:
        if hasattr(x, "to_checksum_address"):
            return str(x)
    except Exception:
        pass
    return x


def get_pool_metadata(w3: Web3, pool: str) -> Dict[str, Any]:
    c = w3.eth.contract(address=w3.to_checksum_address(pool), abi=POOL_VIEW_ABI)
    out: Dict[str, Any] = {"pool": pool.lower()}
    for fn in ["token0", "token1", "tickSpacing", "fee", "liquidity"]:
        try:
            out[fn] = json_safe(getattr(c.functions, fn)().call())
        except Exception as e:
            out[fn + "_error"] = str(e)
    try:
        slot0 = c.functions.slot0().call()
        out["slot0"] = {
            "sqrtPriceX96": int(slot0[0]),
            "tick": int(slot0[1]),
            "observationIndex": int(slot0[2]),
            "observationCardinality": int(slot0[3]),
            "observationCardinalityNext": int(slot0[4]),
            "feeProtocol": int(slot0[5]),
            "unlocked": bool(slot0[6]),
        }
    except Exception as e:
        out["slot0_error"] = str(e)
    return out


def make_topic_map():
    out = {}
    for abi in CLPOOL_EVENT_ABIS:
        topic = event_abi_to_log_topic(abi).hex()
        out[topic.lower()] = abi
    return out


def raw_log_to_json(log: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "address": str(log.get("address", "")).lower(),
        "blockNumber": int(log["blockNumber"]),
        "transactionHash": log["transactionHash"].hex(),
        "transactionIndex": int(log["transactionIndex"]),
        "logIndex": int(log["logIndex"]),
        "removed": bool(log.get("removed", False)),
        "topics": [t.hex() for t in log["topics"]],
        "data": log["data"].hex() if hasattr(log["data"], "hex") else str(log["data"]),
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
            raw[k] = json_safe(v)
    except Exception as e:
        raw["decode_ok"] = False
        raw["decode_error"] = str(e)

    return raw


def save_table(df: pd.DataFrame, base: Path) -> Dict[str, str]:
    out = {}
    csv_path = base.with_suffix(".csv")
    df.to_csv(csv_path, index=False)
    out["csv"] = str(csv_path)
    try:
        pq_path = base.with_suffix(".parquet")
        df.to_parquet(pq_path, index=False)
        out["parquet"] = str(pq_path)
    except Exception as e:
        out["parquet_error"] = str(e)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", required=True)
    ap.add_argument("--time-from", default="")
    ap.add_argument("--time-to", default="")
    ap.add_argument("--from-block", type=int, default=0)
    ap.add_argument("--to-block", type=int, default=0)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--rpc-url", default="")
    ap.add_argument("--rpc-env", default="BASE_RPC_URL")
    ap.add_argument("--chunk-size", type=int, default=5000)
    ap.add_argument("--sleep-s", type=float, default=0.10)
    ap.add_argument("--events", default="Initialize,Mint,Burn,Collect,CollectFees,Swap,Flash,IncreaseObservationCardinalityNext,SetFeeProtocol")
    args = ap.parse_args()

    rpc_url = args.rpc_url or os.getenv(args.rpc_env) or "https://mainnet.base.org"
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 60}))
    if not w3.is_connected():
        raise SystemExit(f"RPC not connected: {rpc_url}")

    pool = w3.to_checksum_address(args.pool)
    latest = int(w3.eth.block_number)

    if args.from_block and args.to_block:
        from_block = int(args.from_block)
        to_block = int(args.to_block)
        from_ts = block_ts(w3, from_block)
        to_ts = block_ts(w3, to_block)
    else:
        if not args.time_from or not args.time_to:
            raise SystemExit("Use either --from-block/--to-block or --time-from/--time-to.")
        from_ts = iso_to_epoch_s(args.time_from)
        to_ts = iso_to_epoch_s(args.time_to)
        from_block = find_block_by_timestamp(w3, from_ts, high=latest)
        to_block = find_block_by_timestamp(w3, to_ts, high=latest)

    if to_block < from_block:
        raise SystemExit("to_block < from_block")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    metadata = get_pool_metadata(w3, args.pool)
    metadata.update({
        "rpc_redacted": rpc_url.split("?")[0],
        "latest_block_at_start": latest,
        "from_block": from_block,
        "to_block": to_block,
        "from_ts": from_ts,
        "to_ts": to_ts,
        "time_from": args.time_from,
        "time_to": args.time_to,
        "chunk_size": args.chunk_size,
    })
    (out_dir / "pool_metadata.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")

    topic_map = make_topic_map()
    wanted = {x.strip() for x in args.events.split(",") if x.strip()}
    wanted_topics = {
        topic: abi
        for topic, abi in topic_map.items()
        if abi["name"] in wanted
    }

    rows: List[Dict[str, Any]] = []
    raw_path = out_dir / "events.raw.jsonl"

    # Cache block timestamps.
    ts_cache: Dict[int, int] = {}

    def get_cached_ts(bn: int) -> int:
        if bn not in ts_cache:
            ts_cache[bn] = block_ts(w3, bn)
        return ts_cache[bn]

    with raw_path.open("w", encoding="utf-8") as raw_f:
        for event_topic, event_abi in wanted_topics.items():
            ev_name = event_abi["name"]
            print(f"[event] {ev_name} topic={event_topic}", file=sys.stderr)

            start = from_block
            while start <= to_block:
                end = min(to_block, start + int(args.chunk_size) - 1)
                params = {
                    "address": pool,
                    "fromBlock": int(start),
                    "toBlock": int(end),
                    "topics": [event_topic],
                }

                try:
                    logs = w3.eth.get_logs(params)
                except Exception as e:
                    # Crude adaptive fallback if provider rejects a large range.
                    if end > start and args.chunk_size > 100:
                        smaller = max(100, int(args.chunk_size // 2))
                        print(f"[WARN] get_logs failed {start}-{end} {ev_name}: {e}; retry with --chunk-size {smaller}", file=sys.stderr)
                    raise

                for log in logs:
                    raw_f.write(json.dumps(raw_log_to_json(log), ensure_ascii=False) + "\n")
                    decoded = decode_log(w3, log, topic_map)
                    bn = int(decoded["blockNumber"])
                    decoded["timestamp"] = get_cached_ts(bn)
                    decoded["datetime_utc"] = dt.datetime.fromtimestamp(decoded["timestamp"], tz=dt.timezone.utc).isoformat()
                    rows.append(decoded)

                print(f"[chunk] event={ev_name} blocks={start}-{end} logs={len(logs)} total={len(rows)}", file=sys.stderr)
                start = end + 1
                if args.sleep_s > 0:
                    time.sleep(float(args.sleep_s))

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["blockNumber", "transactionIndex", "logIndex"], kind="stable").reset_index(drop=True)

    files = save_table(df, out_dir / "events_all")

    summary = {
        "pool": args.pool.lower(),
        "from_block": from_block,
        "to_block": to_block,
        "rows": int(len(df)),
        "event_counts": df["event_type"].value_counts().to_dict() if not df.empty and "event_type" in df.columns else {},
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
