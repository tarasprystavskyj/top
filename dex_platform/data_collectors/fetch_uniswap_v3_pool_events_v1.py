#!/usr/bin/env python3
"""
fetch_uniswap_v3_pool_events_v1.py

Fetch event-level Uniswap V3 pool data for LP/backtesting:
  - swaps
  - mints
  - burns
  - collects
  - transaction gas metadata
  - pool metadata

Primary source:
  The Graph / Uniswap V3 subgraph GraphQL API.

Why this exists:
  OHLCV is not enough for concentrated-liquidity LP backtests.
  You need swaps + liquidity events + tick/sqrtPrice + gas to estimate:
    - active in-range liquidity
    - volume crossing your range
    - fee share
    - gas/rebalance drag
    - inventory/adverse-selection loss

Requirements:
  pip install requests pandas pyarrow

API key:
  export THEGRAPH_API_KEY="..."

Example:
  python3 fetch_uniswap_v3_pool_events_v1.py \
    --pool 0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640 \
    --time-from 2026-03-01T00:00:00Z \
    --time-to   2026-04-01T00:00:00Z \
    --out-dir _data/uniswap_v3_usdc_weth_005_mar2026

Notes:
  - Default subgraph ID below is a public Uniswap V3 mainnet subgraph ID found in Graph Explorer.
  - If it fails, pass --subgraph-id from the Graph Explorer page you want to use.
  - For Base/Arbitrum/Polygon, use that chain's Uniswap V3 subgraph ID.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import pandas as pd


DEFAULT_UNISWAP_V3_MAINNET_SUBGRAPH_ID = "5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV"
DEFAULT_GATEWAY_TEMPLATE = "https://gateway.thegraph.com/api/{api_key}/subgraphs/id/{subgraph_id}"


def iso_to_epoch_s(value: str) -> int:
    s = str(value).strip()
    if not s:
        raise ValueError("empty timestamp")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    x = dt.datetime.fromisoformat(s)
    if x.tzinfo is None:
        x = x.replace(tzinfo=dt.timezone.utc)
    else:
        x = x.astimezone(dt.timezone.utc)
    return int(x.timestamp())


def now_utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def graph_post(endpoint: str, query: str, variables: Dict[str, Any], *, timeout: int = 60, max_retries: int = 6) -> Dict[str, Any]:
    payload = {"query": query, "variables": variables}
    last_err = None
    for attempt in range(max_retries):
        try:
            r = requests.post(endpoint, json=payload, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504):
                wait = min(30, 2 ** attempt)
                print(f"[WARN] Graph status={r.status_code}; retry in {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            r.raise_for_status()
            data = r.json()
            if "errors" in data:
                msg = json.dumps(data["errors"], ensure_ascii=False)[:2000]
                raise RuntimeError(f"GraphQL errors: {msg}")
            if "data" not in data:
                raise RuntimeError(f"GraphQL response without data: {str(data)[:500]}")
            return data["data"]
        except Exception as e:
            last_err = e
            if attempt + 1 >= max_retries:
                break
            wait = min(30, 2 ** attempt)
            print(f"[WARN] request failed: {e}; retry in {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"GraphQL request failed after {max_retries} retries: {last_err}") from last_err


POOL_QUERY = """
query PoolInfo($pool: ID!) {
  pool(id: $pool) {
    id
    createdAtTimestamp
    createdAtBlockNumber
    feeTier
    liquidity
    sqrtPrice
    tick
    token0 { id symbol name decimals }
    token1 { id symbol name decimals }
    token0Price
    token1Price
    volumeUSD
    feesUSD
    txCount
    totalValueLockedUSD
  }
}
"""


EVENT_QUERIES = {
    "swaps": """
query Page($pool: String!, $from: BigInt!, $to: BigInt!, $lastID: String!) {
  swaps(
    first: 1000,
    orderBy: id,
    orderDirection: asc,
    where: { pool: $pool, timestamp_gte: $from, timestamp_lte: $to, id_gt: $lastID }
  ) {
    id
    timestamp
    logIndex
    sender
    recipient
    origin
    amount0
    amount1
    amountUSD
    sqrtPriceX96
    tick
    transaction { id blockNumber timestamp gasUsed gasPrice }
    token0 { id symbol decimals }
    token1 { id symbol decimals }
    pool { id feeTier }
  }
}
""",
    "mints": """
query Page($pool: String!, $from: BigInt!, $to: BigInt!, $lastID: String!) {
  mints(
    first: 1000,
    orderBy: id,
    orderDirection: asc,
    where: { pool: $pool, timestamp_gte: $from, timestamp_lte: $to, id_gt: $lastID }
  ) {
    id
    timestamp
    logIndex
    owner
    sender
    origin
    amount
    amount0
    amount1
    amountUSD
    tickLower
    tickUpper
    transaction { id blockNumber timestamp gasUsed gasPrice }
    token0 { id symbol decimals }
    token1 { id symbol decimals }
    pool { id feeTier }
  }
}
""",
    "burns": """
query Page($pool: String!, $from: BigInt!, $to: BigInt!, $lastID: String!) {
  burns(
    first: 1000,
    orderBy: id,
    orderDirection: asc,
    where: { pool: $pool, timestamp_gte: $from, timestamp_lte: $to, id_gt: $lastID }
  ) {
    id
    timestamp
    logIndex
    owner
    origin
    amount
    amount0
    amount1
    amountUSD
    tickLower
    tickUpper
    transaction { id blockNumber timestamp gasUsed gasPrice }
    token0 { id symbol decimals }
    token1 { id symbol decimals }
    pool { id feeTier }
  }
}
""",
    "collects": """
query Page($pool: String!, $from: BigInt!, $to: BigInt!, $lastID: String!) {
  collects(
    first: 1000,
    orderBy: id,
    orderDirection: asc,
    where: { pool: $pool, timestamp_gte: $from, timestamp_lte: $to, id_gt: $lastID }
  ) {
    id
    timestamp
    logIndex
    owner
    amount0
    amount1
    amountUSD
    tickLower
    tickUpper
    transaction { id blockNumber timestamp gasUsed gasPrice }
    pool { id feeTier }
  }
}
""",
}


def safe_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except Exception:
        return None


def safe_int(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except Exception:
        try:
            return int(float(v))
        except Exception:
            return None


def flatten_event(event_type: str, x: Dict[str, Any]) -> Dict[str, Any]:
    tx = x.get("transaction") or {}
    pool = x.get("pool") or {}
    t0 = x.get("token0") or {}
    t1 = x.get("token1") or {}

    row = {
        "event_type": event_type.rstrip("s"),
        "id": x.get("id"),
        "timestamp": int(x.get("timestamp") or tx.get("timestamp") or 0),
        "block_number": safe_int(tx.get("blockNumber")) or 0,
        "tx_hash": tx.get("id"),
        "log_index": safe_int(x.get("logIndex")) or 0,
        "pool": pool.get("id"),
        "fee_tier": safe_float(pool.get("feeTier")),
        "gas_used": safe_float(tx.get("gasUsed")),
        "gas_price_wei": safe_float(tx.get("gasPrice")),
        "sender": x.get("sender"),
        "recipient": x.get("recipient"),
        "origin": x.get("origin"),
        "owner": x.get("owner"),
        "token0": t0.get("id"),
        "token0_symbol": t0.get("symbol"),
        "token0_decimals": safe_int(t0.get("decimals")),
        "token1": t1.get("id"),
        "token1_symbol": t1.get("symbol"),
        "token1_decimals": safe_int(t1.get("decimals")),
        "amount0": safe_float(x.get("amount0")),
        "amount1": safe_float(x.get("amount1")),
        "amount_usd": safe_float(x.get("amountUSD")),
        "liquidity_amount": safe_float(x.get("amount")),
        "sqrt_price_x96": safe_float(x.get("sqrtPriceX96")),
        "tick": safe_int(x.get("tick")),
        "tick_lower": safe_int(x.get("tickLower")),
        "tick_upper": safe_int(x.get("tickUpper")),
    }
    row["datetime_utc"] = dt.datetime.fromtimestamp(row["timestamp"], tz=dt.timezone.utc).isoformat() if row["timestamp"] else ""
    return row


def fetch_entity_pages(
    endpoint: str,
    entity: str,
    pool: str,
    from_ts: int,
    to_ts: int,
    *,
    sleep_s: float = 0.05,
    max_pages: int = 0,
) -> List[Dict[str, Any]]:
    query = EVENT_QUERIES[entity]
    last_id = ""
    rows: List[Dict[str, Any]] = []
    page = 0

    while True:
        variables = {"pool": pool.lower(), "from": int(from_ts), "to": int(to_ts), "lastID": last_id}
        data = graph_post(endpoint, query, variables)
        batch = data.get(entity) or []
        if not batch:
            break

        rows.extend(batch)
        last_id = batch[-1]["id"]
        page += 1
        print(f"[{entity}] page={page} rows_total={len(rows)} last_id={last_id}", file=sys.stderr)

        if max_pages and page >= max_pages:
            print(f"[WARN] stopped {entity} at max_pages={max_pages}", file=sys.stderr)
            break

        if len(batch) < 1000:
            break
        if sleep_s > 0:
            time.sleep(sleep_s)

    return rows


def save_df(df: pd.DataFrame, path_base: Path, *, parquet: bool = True) -> Dict[str, str]:
    out: Dict[str, str] = {}
    csv_path = path_base.with_suffix(".csv")
    df.to_csv(csv_path, index=False)
    out["csv"] = str(csv_path)

    if parquet:
        try:
            pq_path = path_base.with_suffix(".parquet")
            df.to_parquet(pq_path, index=False)
            out["parquet"] = str(pq_path)
        except Exception as e:
            print(f"[WARN] parquet save failed for {path_base.name}: {e}", file=sys.stderr)

    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", required=True, help="Uniswap V3 pool address, e.g. 0x88e6... for USDC/WETH 0.05% mainnet")
    ap.add_argument("--time-from", required=True, help="ISO timestamp, e.g. 2026-03-01T00:00:00Z")
    ap.add_argument("--time-to", required=True, help="ISO timestamp, e.g. 2026-04-01T00:00:00Z")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--api-key", default="", help="The Graph API key. If empty, reads env var from --api-key-env.")
    ap.add_argument("--api-key-env", default="THEGRAPH_API_KEY")
    ap.add_argument("--subgraph-id", default=DEFAULT_UNISWAP_V3_MAINNET_SUBGRAPH_ID)
    ap.add_argument("--endpoint", default="", help="Full GraphQL endpoint. Overrides api-key/subgraph-id.")
    ap.add_argument("--entities", default="swaps,mints,burns,collects")
    ap.add_argument("--sleep-s", type=float, default=0.05)
    ap.add_argument("--max-pages", type=int, default=0, help="Debug limit per entity. 0 = unlimited.")
    ap.add_argument("--no-parquet", action="store_true")
    args = ap.parse_args()

    pool = args.pool.lower()
    from_ts = iso_to_epoch_s(args.time_from)
    to_ts = iso_to_epoch_s(args.time_to)
    if to_ts <= from_ts:
        raise SystemExit("--time-to must be after --time-from")

    api_key = args.api_key or os.getenv(args.api_key_env, "")
    if args.endpoint:
        endpoint = args.endpoint
    else:
        if not api_key:
            raise SystemExit(
                f"Missing The Graph API key. Set env {args.api_key_env}=... or pass --api-key. "
                "You can also pass --endpoint if you use a custom gateway."
            )
        endpoint = DEFAULT_GATEWAY_TEMPLATE.format(api_key=api_key, subgraph_id=args.subgraph_id)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "created_at_utc": now_utc_iso(),
        "source": "thegraph_uniswap_v3_subgraph",
        "subgraph_id": args.subgraph_id,
        "endpoint_redacted": endpoint.replace(api_key, "***") if api_key else endpoint,
        "pool": pool,
        "time_from": args.time_from,
        "time_to": args.time_to,
        "from_ts": from_ts,
        "to_ts": to_ts,
    }

    print("[pool] fetching metadata", file=sys.stderr)
    pool_data = graph_post(endpoint, POOL_QUERY, {"pool": pool}).get("pool")
    if not pool_data:
        raise SystemExit(f"Pool not found in selected subgraph: {pool}")
    (out_dir / "pool.json").write_text(json.dumps(pool_data, indent=2, ensure_ascii=False), encoding="utf-8")

    entities = [x.strip() for x in args.entities.split(",") if x.strip()]
    valid = set(EVENT_QUERIES)
    bad = [x for x in entities if x not in valid]
    if bad:
        raise SystemExit(f"Unknown entities: {bad}; valid={sorted(valid)}")

    all_flat: List[Dict[str, Any]] = []
    saved_files: Dict[str, Any] = {}

    for entity in entities:
        print(f"[{entity}] fetch begin", file=sys.stderr)
        raw = fetch_entity_pages(
            endpoint,
            entity,
            pool,
            from_ts,
            to_ts,
            sleep_s=args.sleep_s,
            max_pages=args.max_pages,
        )
        raw_path = out_dir / f"{entity}.raw.jsonl"
        with raw_path.open("w", encoding="utf-8") as f:
            for item in raw:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        flat = [flatten_event(entity, x) for x in raw]
        df = pd.DataFrame(flat)
        if not df.empty:
            df = df.sort_values(["block_number", "log_index", "id"], kind="stable")
        files = save_df(df, out_dir / entity, parquet=(not args.no_parquet))
        files["raw_jsonl"] = str(raw_path)
        saved_files[entity] = {"rows": len(df), "files": files}
        all_flat.extend(flat)
        print(f"[{entity}] done rows={len(df)}", file=sys.stderr)

    events = pd.DataFrame(all_flat)
    if not events.empty:
        event_priority = {"mint": 1, "burn": 2, "collect": 3, "swap": 4}
        events["_event_priority"] = events["event_type"].map(event_priority).fillna(9).astype(int)
        events = events.sort_values(["block_number", "log_index", "_event_priority", "id"], kind="stable")
        events = events.drop(columns=["_event_priority"])
    event_files = save_df(events, out_dir / "events_all", parquet=(not args.no_parquet))

    summary = {
        **meta,
        "pool_info": pool_data,
        "entities": saved_files,
        "events_all": {"rows": len(events), "files": event_files},
        "warning": (
            "This dataset is event-level but still not a perfect LP replay by itself. "
            "For high-precision fee accounting, reconstruct tick liquidity state and fee growth, "
            "or query tick snapshots / use archive-node logs."
        ),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
