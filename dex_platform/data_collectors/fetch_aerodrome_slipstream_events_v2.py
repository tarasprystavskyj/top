#!/usr/bin/env python3
from __future__ import annotations

"""
dex_platform/data_collectors/fetch_aerodrome_slipstream_events_v2.py

Aerodrome Slipstream / Velodrome CL-style pool event collector.

v2 fixes over v1:
  1. Parquet-safe conversion for huge uint/int fields.
  2. --no-parquet option.
  3. Retry/backoff for RPC 429 / transient errors.
  4. Adaptive get_logs range splitting.
  5. --only-events / --events support.
  6. Metadata view calls are non-fatal.

Primary target:
  CHECK/USDC Aerodrome Slipstream 2%
  0x5a7b4970b2610aee4776a6944d9f2171ee6060b0

Install:
  python3 -m pip install -r dex_platform/data_collectors/requirements_aerodrome.txt

Example:
  export BASE_RPC_URL="https://mainnet.base.org"

  python3 dex_platform/data_collectors/fetch_aerodrome_slipstream_events_v2.py \
    --pool 0x5a7b4970b2610aee4776a6944d9f2171ee6060b0 \
    --time-from 2026-05-01T00:00:00Z \
    --time-to   2026-05-02T00:00:00Z \
    --out-dir DEX_DATA/aerodrome_slipstream/base_CHECK_USDC_2PCT_recent_2026_05_01_v2 \
    --chunk-size 2000 \
    --min-chunk-size 100 \
    --events Swap,Mint,Burn,Collect
"""

import argparse
import datetime as dt
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

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


# Numeric columns that may exceed Arrow/Pandas C integer limits.
BIG_INT_COLUMNS = {
    "amount", "amount0", "amount1", "paid0", "paid1",
    "sqrtPriceX96", "liquidity", "data",
}
SAFE_NUMERIC_COLUMNS = {
    "blockNumber", "transactionIndex", "logIndex", "timestamp",
    "tick", "tickLower", "tickUpper", "fee", "tickSpacing",
    "from_block", "to_block",
}


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


def json_safe(x: Any) -> Any:
    """Convert Web3/HexBytes/AttributeDict values into JSON-safe values."""
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


def as_str_or_none(x: Any) -> Any:
    if x is None:
        return None
    return str(x)


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
            msg = str(e)
            wait = min(max_sleep, base_sleep * (2 ** i)) + random.random() * 0.25
            print(f"[WARN] {label} failed attempt={i+1}/{attempts}: {msg}; sleep {wait:.2f}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"{label} failed after {attempts} attempts: {last_exc}") from last_exc


def block_ts(w3: Web3, block_number: int, *, attempts: int = 6) -> int:
    block = retry_call(
        lambda: w3.eth.get_block(int(block_number)),
        label=f"get_block({block_number})",
        attempts=attempts,
    )
    return int(block["timestamp"])


def find_block_by_timestamp(w3: Web3, target_ts: int, *, low: int = 0, high: Optional[int] = None) -> int:
    """Find first block with timestamp >= target_ts."""
    if high is None:
        high = int(retry_call(lambda: w3.eth.block_number, label="eth.block_number"))

    lo = int(low)
    hi = int(high)

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


def get_pool_metadata(w3: Web3, pool: str) -> Dict[str, Any]:
    c = w3.eth.contract(address=w3.to_checksum_address(pool), abi=POOL_VIEW_ABI)
    out: Dict[str, Any] = {"pool": pool.lower()}

    for fn_name in ["token0", "token1", "tickSpacing", "fee", "liquidity"]:
        try:
            val = retry_call(
                lambda fn_name=fn_name: getattr(c.functions, fn_name)().call(),
                label=f"pool.{fn_name}()",
                attempts=4,
            )
            out[fn_name] = json_safe(val)
        except Exception as e:
            out[fn_name + "_error"] = str(e)

    try:
        slot0 = retry_call(lambda: c.functions.slot0().call(), label="pool.slot0()", attempts=4)
        out["slot0"] = {
            "sqrtPriceX96": str(slot0[0]),
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
            if k in BIG_INT_COLUMNS:
                raw[k] = as_str_or_none(val)
            else:
                raw[k] = val
    except Exception as e:
        raw["decode_ok"] = False
        raw["decode_error"] = str(e)

    return raw


def parquet_safe_df(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy safe for pyarrow parquet.

    Large uint/int fields are cast to string to avoid:
      Python int too large to convert to C long
    """
    out = df.copy()

    for col in out.columns:
        if col in BIG_INT_COLUMNS:
            out[col] = out[col].map(lambda x: None if pd.isna(x) else str(x))
        elif col in {"topics"}:
            out[col] = out[col].map(lambda x: json.dumps(x, ensure_ascii=False) if not isinstance(x, str) else x)
        elif col in SAFE_NUMERIC_COLUMNS:
            out[col] = pd.to_numeric(out[col], errors="coerce")
        elif col.endswith("_error"):
            out[col] = out[col].astype("string")
        elif col in {"transactionHash", "address", "owner", "sender", "recipient", "event_type", "datetime_utc", "decode_error"}:
            out[col] = out[col].astype("string")

    return out


def save_table(df: pd.DataFrame, base: Path, *, write_parquet: bool = True) -> Dict[str, str]:
    out: Dict[str, str] = {}

    csv_path = base.with_suffix(".csv")
    df.to_csv(csv_path, index=False)
    out["csv"] = str(csv_path)

    if write_parquet:
        try:
            pq_df = parquet_safe_df(df)
            pq_path = base.with_suffix(".parquet")
            pq_df.to_parquet(pq_path, index=False)
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
    """Fetch logs with recursive range splitting on provider errors."""
    params = {
        "address": address,
        "fromBlock": int(from_block),
        "toBlock": int(to_block),
        "topics": [topic0],
    }

    try:
        logs = retry_call(
            lambda: w3.eth.get_logs(params),
            label=f"get_logs({from_block}-{to_block})",
            attempts=attempts,
            base_sleep=1.0,
        )
        return list(logs)
    except Exception as e:
        size = int(to_block) - int(from_block) + 1
        if size <= int(min_chunk_size):
            raise RuntimeError(f"get_logs failed at min range {from_block}-{to_block}: {e}") from e

        mid = (int(from_block) + int(to_block)) // 2
        print(f"[WARN] splitting get_logs range {from_block}-{to_block} -> {from_block}-{mid}, {mid+1}-{to_block}", file=sys.stderr)
        left = get_logs_adaptive(
            w3,
            address=address,
            topic0=topic0,
            from_block=from_block,
            to_block=mid,
            min_chunk_size=min_chunk_size,
            attempts=attempts,
        )
        right = get_logs_adaptive(
            w3,
            address=address,
            topic0=topic0,
            from_block=mid + 1,
            to_block=to_block,
            min_chunk_size=min_chunk_size,
            attempts=attempts,
        )
        return left + right


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
    ap.add_argument("--chunk-size", type=int, default=2000)
    ap.add_argument("--min-chunk-size", type=int, default=100)
    ap.add_argument("--sleep-s", type=float, default=0.10)
    ap.add_argument("--attempts", type=int, default=6)
    ap.add_argument("--events", "--only-events", dest="events", default="Swap,Mint,Burn,Collect")
    ap.add_argument("--no-parquet", action="store_true")
    args = ap.parse_args()

    rpc_url = args.rpc_url or os.getenv(args.rpc_env) or "https://mainnet.base.org"
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 90}))

    if not w3.is_connected():
        raise SystemExit(f"RPC not connected: {rpc_url}")

    pool = w3.to_checksum_address(args.pool)
    latest = int(retry_call(lambda: w3.eth.block_number, label="eth.block_number", attempts=args.attempts))

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
        "min_chunk_size": args.min_chunk_size,
        "collector_version": "v2",
    })
    (out_dir / "pool_metadata.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")

    topic_map = make_topic_map()
    wanted = {x.strip() for x in args.events.split(",") if x.strip()}

    wanted_topics: Dict[str, Dict[str, Any]] = {}
    for topic, abi in topic_map.items():
        if abi["name"] in wanted:
            wanted_topics[topic] = abi

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
                    decoded["datetime_utc"] = dt.datetime.fromtimestamp(decoded["timestamp"], tz=dt.timezone.utc).isoformat()
                    rows.append(decoded)

                total_ev_logs += len(logs)
                print(f"[chunk] event={ev_name} blocks={start}-{end} logs={len(logs)} event_total={total_ev_logs} all_total={len(rows)}", file=sys.stderr)
                start = end + 1

                if args.sleep_s > 0:
                    time.sleep(float(args.sleep_s))

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["blockNumber", "transactionIndex", "logIndex"], kind="stable").reset_index(drop=True)

    files = save_table(df, out_dir / "events_all", write_parquet=not args.no_parquet)

    summary = {
        "pool": args.pool.lower(),
        "collector_version": "v2",
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
