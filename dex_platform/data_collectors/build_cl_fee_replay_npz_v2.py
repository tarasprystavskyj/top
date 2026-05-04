#!/usr/bin/env python3
from __future__ import annotations

"""
dex_platform/data_collectors/build_cl_fee_replay_npz_v2.py

Build compact NPZ cache from CL pool events_all.csv/parquet for fast DEX LP tuning.

v2:
- full file, not a patch
- SCRIPT_VERSION output
- keeps only Swap arrays needed for tuning
- stores metadata JSON inside NPZ and next to NPZ
"""

import argparse
import json
import math
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd


SCRIPT_VERSION = "build_cl_fee_replay_npz_v2_2026_05_02"


def print_version() -> None:
    print(f"[script_version] {__file__} SCRIPT_VERSION={SCRIPT_VERSION}")


def parse_int_safe(x: Any) -> Optional[int]:
    if x is None:
        return None
    try:
        if pd.isna(x):
            return None
    except Exception:
        pass
    if isinstance(x, (int, np.integer)):
        return int(x)
    if isinstance(x, float):
        if not math.isfinite(x):
            return None
        return int(x)
    s = str(x).strip()
    if not s or s.lower() == "nan":
        return None
    try:
        return int(s)
    except Exception:
        return int(float(s))


def read_events(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise SystemExit(f"events file not found: {path}")
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    return pd.read_csv(path)


def price_token0_per_token1_from_sqrt(sqrt_price_x96: int, dec0: int, dec1: int) -> float:
    q_raw_token1_per_token0 = (int(sqrt_price_x96) / (2 ** 96)) ** 2
    return (10 ** (dec1 - dec0)) / q_raw_token1_per_token0


def swap_input_value_usd(amount0_raw: int, amount1_raw: int, price0_per_1: float, dec0: int, dec1: int, quote_token: str) -> float:
    amount0 = int(amount0_raw)
    amount1 = int(amount1_raw)

    quote_token = str(quote_token).lower()
    if quote_token == "token0":
        if amount0 > 0:
            return amount0 / (10 ** dec0)
        if amount1 > 0:
            return (amount1 / (10 ** dec1)) * price0_per_1
        return max(abs(amount0) / (10 ** dec0), abs(amount1) / (10 ** dec1) * price0_per_1)

    if quote_token == "token1":
        if amount1 > 0:
            return amount1 / (10 ** dec1)
        if amount0 > 0:
            return (amount0 / (10 ** dec0)) / max(price0_per_1, 1e-30)
        return max(abs(amount1) / (10 ** dec1), abs(amount0) / (10 ** dec0) / max(price0_per_1, 1e-30))

    raise ValueError("--quote-token must be token0 or token1")


def main() -> None:
    print_version()

    ap = argparse.ArgumentParser()
    ap.add_argument("--events", required=True)
    ap.add_argument("--out-npz", required=True)
    ap.add_argument("--pool-name", default="")
    ap.add_argument("--token0", default="USDC")
    ap.add_argument("--token1", default="CHECK")
    ap.add_argument("--dec0", type=int, default=6)
    ap.add_argument("--dec1", type=int, default=18)
    ap.add_argument("--quote-token", choices=["token0", "token1"], default="token0")
    ap.add_argument("--fee-rate", type=float, default=0.002515)
    args = ap.parse_args()

    events_path = Path(args.events)
    out_npz = Path(args.out_npz)
    out_npz.parent.mkdir(parents=True, exist_ok=True)

    df = read_events(events_path)
    if "event_type" not in df.columns:
        raise SystemExit("events file misses event_type column")

    sw = df[df["event_type"].astype(str).str.lower() == "swap"].copy()
    if sw.empty:
        raise SystemExit("no Swap rows found")

    required = ["timestamp", "blockNumber", "logIndex", "tick", "amount0", "amount1", "sqrtPriceX96", "liquidity"]
    missing = [c for c in required if c not in sw.columns]
    if missing:
        raise SystemExit(f"missing required columns: {missing}")

    for col in ["timestamp", "blockNumber", "logIndex", "tick"]:
        sw[col] = pd.to_numeric(sw[col], errors="coerce")

    for col in ["amount0", "amount1", "sqrtPriceX96", "liquidity"]:
        sw[col] = sw[col].map(parse_int_safe)

    sw = sw.dropna(subset=required)
    sw = sw.sort_values(["timestamp", "blockNumber", "logIndex"], kind="stable").reset_index(drop=True)

    n = len(sw)
    ts = sw["timestamp"].astype(np.int64).to_numpy()
    block = sw["blockNumber"].astype(np.int64).to_numpy()
    log_index = sw["logIndex"].astype(np.int32).to_numpy()
    tick = sw["tick"].astype(np.int32).to_numpy()

    sqrt_vals = sw["sqrtPriceX96"].tolist()
    amount0_raw = sw["amount0"].tolist()
    amount1_raw = sw["amount1"].tolist()
    liq_raw = sw["liquidity"].tolist()

    price = np.empty(n, dtype=np.float64)
    amount0_h = np.empty(n, dtype=np.float64)
    amount1_h = np.empty(n, dtype=np.float64)
    input_usd = np.empty(n, dtype=np.float64)
    active_liq = np.empty(n, dtype=np.float64)

    for i in range(n):
        p = price_token0_per_token1_from_sqrt(int(sqrt_vals[i]), args.dec0, args.dec1)
        price[i] = p
        amount0_h[i] = int(amount0_raw[i]) / (10 ** args.dec0)
        amount1_h[i] = int(amount1_raw[i]) / (10 ** args.dec1)
        active_liq[i] = max(float(liq_raw[i]), 1.0)
        input_usd[i] = swap_input_value_usd(int(amount0_raw[i]), int(amount1_raw[i]), p, args.dec0, args.dec1, args.quote_token)

    meta = {
        "script_version": SCRIPT_VERSION,
        "source_events": str(events_path),
        "pool_name": args.pool_name,
        "token0": args.token0,
        "token1": args.token1,
        "dec0": args.dec0,
        "dec1": args.dec1,
        "quote_token": args.quote_token,
        "fee_rate": args.fee_rate,
        "rows_swap": int(n),
        "timestamp_start": int(ts[0]),
        "timestamp_end": int(ts[-1]),
        "price_start": float(price[0]),
        "price_end": float(price[-1]),
        "price_min": float(np.nanmin(price)),
        "price_max": float(np.nanmax(price)),
        "input_usd_sum": float(np.nansum(input_usd)),
    }

    np.savez_compressed(
        out_npz,
        ts=ts,
        block=block,
        log_index=log_index,
        tick=tick,
        price=price,
        amount0_h=amount0_h,
        amount1_h=amount1_h,
        input_usd=input_usd,
        active_liquidity=active_liq,
        meta_json=np.array(json.dumps(meta, ensure_ascii=False), dtype=np.unicode_),
    )

    meta_path = out_npz.with_suffix(".meta.json")
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps({
        "out_npz": str(out_npz),
        "meta": str(meta_path),
        "rows_swap": int(n),
        "input_usd_sum": float(np.nansum(input_usd)),
        "price_start": float(price[0]),
        "price_end": float(price[-1]),
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
