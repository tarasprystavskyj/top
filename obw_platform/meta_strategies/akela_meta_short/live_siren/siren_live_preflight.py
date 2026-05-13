#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import subprocess
from pathlib import Path

import ccxt
import yaml

ROOT = Path(__file__).resolve().parents[4]
LANE = ROOT / "obw_platform" / "meta_strategies" / "akela_meta_short" / "live_siren"
DEFAULT_CFG = LANE / "V21_siren_bingx_live_s0.yaml"
DEFAULT_OUT = ROOT / "_reports" / "akela_meta_short" / "siren_live_prep" / "siren_s0_preflight.json"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_hash() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(ROOT), text=True).strip()
    except Exception:
        return ""


def depth_notional(levels, mid: float, bp: float) -> float:
    if mid <= 0:
        return 0.0
    total = 0.0
    low = mid * (1.0 - bp / 10000.0)
    high = mid * (1.0 + bp / 10000.0)
    for px, qty in levels or []:
        try:
            p = float(px)
            q = float(qty)
        except Exception:
            continue
        if low <= p <= high:
            total += p * q
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cfg", default=str(DEFAULT_CFG))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--exchange", default="bingx")
    args = ap.parse_args()

    cfg_path = Path(args.cfg)
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    symbol = cfg.get("symbol") or "FREEDOMMONEY/USDT:USDT"
    telemetry = ((cfg.get("runner") or {}).get("s0_micro_telemetry") or {})
    floor_cfg = telemetry.get("dynamic_min_order_floor") or {}
    configured = float(floor_cfg.get("configured_min_order_usdt") or (cfg.get("strategy_params_long") or {}).get("minOrderUSDT") or 0.0)
    buffer = float(floor_cfg.get("buffer") or 1.10)

    ex = getattr(ccxt, args.exchange)({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    ex.load_markets()
    market = ex.market(symbol)
    ticker = ex.fetch_ticker(symbol)
    book = ex.fetch_order_book(symbol, 10)

    price = float(ticker.get("last") or ticker.get("close") or ticker.get("bid") or ticker.get("ask") or 0.0)
    min_amount = float(((market.get("limits") or {}).get("amount") or {}).get("min") or 0.0)
    min_cost = float(((market.get("limits") or {}).get("cost") or {}).get("min") or 0.0)
    effective = max(configured, min_cost * buffer if min_cost > 0 else 0.0, min_amount * price * buffer if min_amount > 0 and price > 0 else 0.0)

    bids = book.get("bids") or []
    asks = book.get("asks") or []
    best_bid = float(bids[0][0]) if bids else 0.0
    best_ask = float(asks[0][0]) if asks else 0.0
    mid = (best_bid + best_ask) / 2.0 if best_bid > 0 and best_ask > 0 else price
    spread_bp = ((best_ask - best_bid) / mid) * 10000.0 if mid > 0 and best_ask > 0 else None

    payload = {
        "schema": "siren_s0_preflight_v1",
        "ts_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "cfg": str(cfg_path.relative_to(ROOT)),
        "cfg_sha256": sha256_file(cfg_path),
        "git_hash": git_hash(),
        "exchange": args.exchange,
        "symbol": symbol,
        "mode_label": (cfg.get("runner") or {}).get("mode_label"),
        "price": price,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread_bp": spread_bp,
        "min_amount": min_amount,
        "min_cost": min_cost,
        "configured_min_order_usdt": configured,
        "dynamic_floor_buffer": buffer,
        "effective_min_order_usdt": effective,
        "top5_bid_notional": sum(float(px) * float(qty) for px, qty in bids[:5]),
        "top5_ask_notional": sum(float(px) * float(qty) for px, qty in asks[:5]),
        "depth_1bp_bid_notional": depth_notional(bids, mid, 1.0),
        "depth_1bp_ask_notional": depth_notional(asks, mid, 1.0),
        "depth_5bp_bid_notional": depth_notional(bids, mid, 5.0),
        "depth_5bp_ask_notional": depth_notional(asks, mid, 5.0),
        "depth_10bp_bid_notional": depth_notional(bids, mid, 10.0),
        "depth_10bp_ask_notional": depth_notional(asks, mid, 10.0),
        "private_api_permissions_verified": False,
        "private_api_note": "Runner auth probe verifies private access at live start without printing secrets.",
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
