#!/usr/bin/env python3
from __future__ import annotations

"""
dex_platform/data_collectors/fetch_cl_pool_events_etherscan_v1.py

Etherscan V2 logs collector for EVM concentrated-liquidity pools.

Why:
  Public BSC RPC returns:
    {'code': -32005, 'message': 'limit exceeded'}
  even for single-block eth_getLogs queries on very active pools.
  This collector uses Etherscan V2 getLogs endpoint instead of eth_getLogs.

Targets:
  - BSC / chainid 56 QUG/USDT pool:
    0xe1acb466421ed24dd8bd381d1205bad0ad43ca9c

Requirements:
  python3 -m pip install requests web3 pandas pyarrow

API key:
  export ETHERSCAN_API_KEY="..."
  Etherscan API V2 uses one key across supported chains.
  BNB Smart Chain Mainnet may require a paid Etherscan API tier.

Optional RPC:
  export BSC_RPC_URL="https://bsc-dataseed.binance.org/"
  Used only to convert timestamps to block numbers.
  Logs are fetched through Etherscan, not RPC.

Example:
  python3 dex_platform/data_collectors/fetch_cl_pool_events_etherscan_v1.py \
    --chain-id 56 \
    --address 0xe1acb466421ed24dd8bd381d1205bad0ad43ca9c \
    --time-from 2026-05-01T00:00:00Z \
    --time-to   2026-05-01T01:00:00Z \
    --rpc-env BSC_RPC_URL \
    --out-dir DEX_DATA/uniswap_v3_bsc/QUG_USDT_001_2026_05_01_1h_etherscan_v1 \
    --events Swap \
    --block-chunk 50
"""

import argparse
import datetime as dt
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
from eth_utils import event_abi_to_log_topic
from hexbytes import HexBytes
from web3 import Web3
from web3._utils.events import get_event_data


SCRIPT_VERSION = "fetch_cl_pool_events_etherscan_v1_2026_05_02_full_file"
ETHERSCAN_V2_ENDPOINT = "https://api.etherscan.io/v2/api"


CLPOOL_EVENT_ABIS: List[Dict[str, Any]] = [
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
]

BIG_INT_COLUMNS = {"amount", "amount0", "amount1", "sqrtPriceX96", "liquidity", "data"}
SAFE_NUMERIC_COLUMNS = {"blockNumber", "transactionIndex", "logIndex", "timestamp", "tick", "tickLower", "tickUpper"}


def print_version() -> None:
    print(f"[script_version] {__file__} SCRIPT_VERSION={SCRIPT_VERSION}", file=sys.stderr)


def inject_poa_middleware(w3: Web3, *, enabled: bool = True) -> str:
    if not enabled:
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
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    x = dt.datetime.fromisoformat(s)
    if x.tzinfo is None:
        x = x.replace(tzinfo=dt.timezone.utc)
    else:
        x = x.astimezone(dt.timezone.utc)
    return int(x.timestamp())


def int_any(x: Any) -> int:
    if isinstance(x, int):
        return x
    s = str(x).strip()
    if s.startswith("0x"):
        return int(s, 16)
    return int(s)


def make_topic_map() -> Dict[str, Dict[str, Any]]:
    out = {}
    for abi in CLPOOL_EVENT_ABIS:
        topic = event_abi_to_log_topic(abi).hex()
        if not topic.startswith("0x"):
            topic = "0x" + topic
        out[topic.lower()] = abi
    return out


def json_safe(x: Any) -> Any:
    if isinstance(x, (bytes, bytearray, HexBytes)):
        return "0x" + x.hex().replace("0x", "")
    if isinstance(x, dict):
        return {str(k): json_safe(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [json_safe(v) for v in x]
    return x


def block_ts(w3: Web3, block_number: int) -> int:
    return int(w3.eth.get_block(int(block_number))["timestamp"])


def find_block_by_timestamp_rpc(w3: Web3, target_ts: int) -> int:
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


def make_rpc(rpc_url: str, *, no_poa: bool) -> Optional[Web3]:
    if not rpc_url:
        return None
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 60}))
    inject_poa_middleware(w3, enabled=not no_poa)
    if not w3.is_connected():
        raise SystemExit(f"RPC not connected: {rpc_url.split('?')[0]}")
    return w3


def normalize_etherscan_log(log: Dict[str, Any]) -> Dict[str, Any]:
    # Etherscan V2 returns mostly Ethereum JSON-RPC-like fields.
    topics = log.get("topics") or []
    return {
        "address": str(log.get("address", "")).lower(),
        "blockNumber": int_any(log.get("blockNumber", 0)),
        "transactionHash": str(log.get("transactionHash", "")),
        "transactionIndex": int_any(log.get("transactionIndex", 0)),
        "logIndex": int_any(log.get("logIndex", 0)),
        "removed": bool(log.get("removed", False)),
        "topics": [str(t) for t in topics],
        "data": str(log.get("data", "0x")),
        "timeStamp": log.get("timeStamp") or log.get("timestamp"),
    }


def to_web3_log(n: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "address": Web3.to_checksum_address(n["address"]),
        "blockNumber": int(n["blockNumber"]),
        "transactionHash": HexBytes(n["transactionHash"]),
        "transactionIndex": int(n["transactionIndex"]),
        "logIndex": int(n["logIndex"]),
        "removed": bool(n.get("removed", False)),
        "topics": [HexBytes(t) for t in n["topics"]],
        "data": HexBytes(n["data"]),
    }


def decode_log(web3_codec: Any, nlog: Dict[str, Any], topic_map: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    out = dict(nlog)
    topic0 = out["topics"][0].lower() if out.get("topics") else ""
    abi = topic_map.get(topic0)

    if not abi:
        out["event_type"] = "UNKNOWN"
        out["decode_ok"] = False
        out["decode_error"] = "unknown topic0"
        return out

    out["event_type"] = abi["name"]
    try:
        decoded = get_event_data(web3_codec, abi, to_web3_log(nlog))
        out["decode_ok"] = True
        args = dict(decoded.get("args", {}))
        for k, v in args.items():
            val = json_safe(v)
            out[k] = str(val) if k in BIG_INT_COLUMNS else val
    except Exception as e:
        out["decode_ok"] = False
        out["decode_error"] = str(e)

    return out


def etherscan_get_logs(
    *,
    session: requests.Session,
    api_key: str,
    chain_id: int,
    address: str,
    topic0: str,
    from_block: int,
    to_block: int,
    attempts: int,
    sleep_s: float,
) -> List[Dict[str, Any]]:
    params = {
        "chainid": str(chain_id),
        "module": "logs",
        "action": "getLogs",
        "fromBlock": str(int(from_block)),
        "toBlock": str(int(to_block)),
        "address": address,
        "topic0": topic0,
        "apikey": api_key,
    }

    last_payload: Any = None
    for i in range(max(1, attempts)):
        try:
            r = session.get(ETHERSCAN_V2_ENDPOINT, params=params, timeout=60)
            payload = r.json()
            last_payload = payload
        except Exception as e:
            wait = min(30, 1.5 * (2 ** i)) + random.random() * 0.25
            print(f"[WARN] etherscan http/json failed {from_block}-{to_block} attempt={i+1}: {e}; sleep {wait:.2f}s", file=sys.stderr)
            time.sleep(wait)
            continue

        status = str(payload.get("status", ""))
        message = str(payload.get("message", ""))
        result = payload.get("result")

        if isinstance(result, list):
            return result

        text = f"{status} {message} {result}".lower()

        if "no records" in text or "no record" in text:
            return []

        if any(x in text for x in ["rate limit", "max rate", "too many", "timeout", "busy"]):
            wait = min(60, 2.0 * (2 ** i)) + random.random() * 0.25
            print(f"[WARN] etherscan rate/busy {from_block}-{to_block} attempt={i+1}: {payload}; sleep {wait:.2f}s", file=sys.stderr)
            time.sleep(wait)
            continue

        # Some Etherscan errors mean the block range is too broad or tier is wrong.
        raise RuntimeError(f"Etherscan getLogs error for {from_block}-{to_block}: {payload}")

    raise RuntimeError(f"Etherscan getLogs failed after attempts for {from_block}-{to_block}: {last_payload}")


def split_fetch_logs(
    *,
    session: requests.Session,
    api_key: str,
    chain_id: int,
    address: str,
    topic0: str,
    from_block: int,
    to_block: int,
    min_chunk: int,
    attempts: int,
    sleep_s: float,
    max_logs_per_chunk: int,
    failed_ranges: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    try:
        logs = etherscan_get_logs(
            session=session,
            api_key=api_key,
            chain_id=chain_id,
            address=address,
            topic0=topic0,
            from_block=from_block,
            to_block=to_block,
            attempts=attempts,
            sleep_s=sleep_s,
        )

        if max_logs_per_chunk > 0 and len(logs) >= max_logs_per_chunk and from_block < to_block:
            raise RuntimeError(f"too many logs in chunk: {len(logs)} >= {max_logs_per_chunk}")

        return logs

    except Exception as e:
        size = int(to_block) - int(from_block) + 1
        if size > int(min_chunk):
            mid = (int(from_block) + int(to_block)) // 2
            print(f"[split] range={from_block}-{to_block} size={size} reason={str(e)[:180]}", file=sys.stderr)
            return (
                split_fetch_logs(
                    session=session,
                    api_key=api_key,
                    chain_id=chain_id,
                    address=address,
                    topic0=topic0,
                    from_block=from_block,
                    to_block=mid,
                    min_chunk=min_chunk,
                    attempts=attempts,
                    sleep_s=sleep_s,
                    max_logs_per_chunk=max_logs_per_chunk,
                    failed_ranges=failed_ranges,
                )
                + split_fetch_logs(
                    session=session,
                    api_key=api_key,
                    chain_id=chain_id,
                    address=address,
                    topic0=topic0,
                    from_block=mid + 1,
                    to_block=to_block,
                    min_chunk=min_chunk,
                    attempts=attempts,
                    sleep_s=sleep_s,
                    max_logs_per_chunk=max_logs_per_chunk,
                    failed_ranges=failed_ranges,
                )
            )

        failed = {
            "topic0": topic0,
            "from_block": int(from_block),
            "to_block": int(to_block),
            "error": str(e),
        }
        failed_ranges.append(failed)
        print(f"[FAILED_RANGE] {failed}", file=sys.stderr)
        return []


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


def main() -> None:
    print_version()

    ap = argparse.ArgumentParser()
    ap.add_argument("--chain-id", type=int, required=True)
    ap.add_argument("--address", "--pool", dest="address", required=True)
    ap.add_argument("--api-key-env", default="ETHERSCAN_API_KEY")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--rpc-url", default="")
    ap.add_argument("--rpc-env", default="")
    ap.add_argument("--time-from", default="")
    ap.add_argument("--time-to", default="")
    ap.add_argument("--from-block", type=int, default=0)
    ap.add_argument("--to-block", type=int, default=0)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--events", default="Swap")
    ap.add_argument("--block-chunk", type=int, default=50)
    ap.add_argument("--min-block-chunk", type=int, default=1)
    ap.add_argument("--attempts", type=int, default=4)
    ap.add_argument("--sleep-s", type=float, default=0.25)
    ap.add_argument("--max-logs-per-chunk", type=int, default=900)
    ap.add_argument("--no-parquet", action="store_true")
    ap.add_argument("--no-poa", action="store_true")
    args = ap.parse_args()

    api_key = args.api_key or os.getenv(args.api_key_env, "")
    if not api_key:
        raise SystemExit(f"{args.api_key_env} is not set. Create an Etherscan API V2 key and export it.")

    w3: Optional[Web3] = None
    rpc_url = args.rpc_url or (os.getenv(args.rpc_env, "") if args.rpc_env else "")
    if rpc_url:
        w3 = make_rpc(rpc_url, no_poa=args.no_poa)
        print(f"[rpc] connected for time->block conversion chain_id={w3.eth.chain_id}", file=sys.stderr)

    if args.from_block and args.to_block:
        from_block = int(args.from_block)
        to_block = int(args.to_block)
        from_ts = None
        to_ts = None
    else:
        if not w3:
            raise SystemExit("Use --from-block/--to-block or provide --rpc-env/--rpc-url for timestamp conversion.")
        if not args.time_from or not args.time_to:
            raise SystemExit("Use either --from-block/--to-block or --time-from/--time-to.")
        from_ts = iso_to_epoch_s(args.time_from)
        to_ts = iso_to_epoch_s(args.time_to)
        from_block = find_block_by_timestamp_rpc(w3, from_ts)
        to_block = find_block_by_timestamp_rpc(w3, to_ts)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    topic_map = make_topic_map()
    selected_events = [x.strip() for x in args.events.split(",") if x.strip()]
    selected_topics: List[Tuple[str, Dict[str, Any]]] = []
    for event_name in selected_events:
        found = False
        for topic, abi in topic_map.items():
            if abi["name"] == event_name:
                selected_topics.append((topic, abi))
                found = True
                break
        if not found:
            raise SystemExit(f"Unknown event: {event_name}")

    metadata = {
        "script_version": SCRIPT_VERSION,
        "endpoint": ETHERSCAN_V2_ENDPOINT,
        "chain_id": args.chain_id,
        "address": args.address.lower(),
        "api_key_env": args.api_key_env,
        "rpc_env": args.rpc_env,
        "from_block": from_block,
        "to_block": to_block,
        "time_from": args.time_from,
        "time_to": args.time_to,
        "events": selected_events,
        "block_chunk": args.block_chunk,
        "min_block_chunk": args.min_block_chunk,
        "max_logs_per_chunk": args.max_logs_per_chunk,
    }
    (out_dir / "metadata.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")

    codec_w3 = Web3()
    session = requests.Session()
    all_rows: List[Dict[str, Any]] = []
    failed_ranges: List[Dict[str, Any]] = []

    raw_path = out_dir / "etherscan_raw_logs.jsonl"
    with raw_path.open("w", encoding="utf-8") as raw_f:
        for topic0, abi in selected_topics:
            event_name = abi["name"]
            event_rows: List[Dict[str, Any]] = []
            print(f"[event] {event_name} topic={topic0}", file=sys.stderr)

            start = int(from_block)
            while start <= int(to_block):
                end = min(int(to_block), start + int(args.block_chunk) - 1)

                logs = split_fetch_logs(
                    session=session,
                    api_key=api_key,
                    chain_id=args.chain_id,
                    address=args.address,
                    topic0=topic0,
                    from_block=start,
                    to_block=end,
                    min_chunk=args.min_block_chunk,
                    attempts=args.attempts,
                    sleep_s=args.sleep_s,
                    max_logs_per_chunk=args.max_logs_per_chunk,
                    failed_ranges=failed_ranges,
                )

                for raw in logs:
                    raw_f.write(json.dumps(raw, ensure_ascii=False) + "\n")
                    nlog = normalize_etherscan_log(raw)
                    row = decode_log(codec_w3.codec, nlog, topic_map)

                    ts_val = nlog.get("timeStamp")
                    ts_int: Optional[int] = None
                    if ts_val is not None and str(ts_val) not in ("", "None"):
                        ts_int = int_any(ts_val)

                    row["timestamp"] = ts_int
                    row["datetime_utc"] = pd.to_datetime(ts_int, unit="s", utc=True).isoformat() if ts_int else ""

                    event_rows.append(row)
                    all_rows.append(row)

                print(
                    f"[chunk] event={event_name} blocks={start}-{end} "
                    f"logs={len(logs)} event_total={len(event_rows)} all_total={len(all_rows)}",
                    file=sys.stderr,
                )

                start = end + 1
                time.sleep(args.sleep_s)

            ev_df = pd.DataFrame(event_rows)
            if not ev_df.empty:
                ev_df = ev_df.sort_values(["blockNumber", "transactionIndex", "logIndex"], kind="stable").reset_index(drop=True)
            save_table(ev_df, out_dir / f"events_{event_name.lower()}_checkpoint", write_parquet=not args.no_parquet)

    df = pd.DataFrame(all_rows)
    if not df.empty:
        df = df.sort_values(["blockNumber", "transactionIndex", "logIndex"], kind="stable").reset_index(drop=True)

    files = save_table(df, out_dir / "events_all", write_parquet=not args.no_parquet)

    failed_path = out_dir / "failed_ranges.json"
    failed_path.write_text(json.dumps(failed_ranges, indent=2, ensure_ascii=False), encoding="utf-8")

    summary = {
        "script_version": SCRIPT_VERSION,
        "chain_id": args.chain_id,
        "address": args.address.lower(),
        "from_block": from_block,
        "to_block": to_block,
        "rows": int(len(df)),
        "event_counts": df["event_type"].value_counts().to_dict() if not df.empty and "event_type" in df.columns else {},
        "decode_ok_counts": df["decode_ok"].value_counts(dropna=False).to_dict() if not df.empty and "decode_ok" in df.columns else {},
        "failed_ranges": len(failed_ranges),
        "files": {
            **files,
            "raw_jsonl": str(raw_path),
            "metadata": str(out_dir / "metadata.json"),
            "failed_ranges": str(failed_path),
        },
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
