#!/usr/bin/env python3
"""Minimal Gate.io live fill probe for slippage telemetry.

This script is intentionally narrow:
- Gate.io USDT swap only.
- One market entry at computed minimum size.
- Optional immediate reduce-only close.
- Writes pre/post orderbook, order/fill data, and fee fields to a JSON report.

It reads API credentials from environment variables but never prints them.
By default it is dry-run. A real order requires both:
  --execute-live
  --i-accept-real-order
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def finite(raw: Any) -> Optional[float]:
    try:
        value = float(raw)
    except Exception:
        return None
    return value if math.isfinite(value) and value > 0 else None


def env_first(*names: str) -> Optional[str]:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def compact_book(book: Dict[str, Any], limit: int = 5) -> Dict[str, Any]:
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    best_bid = finite(bids[0][0]) if bids else None
    best_ask = finite(asks[0][0]) if asks else None
    mid = (best_bid + best_ask) / 2.0 if best_bid and best_ask else None
    return {
        "timestamp_utc": utc_now(),
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread_bp": ((best_ask - best_bid) / mid * 10000.0) if best_bid and best_ask and mid else None,
        "bids_top": [[float(px), float(qty)] for px, qty in bids[:limit]],
        "asks_top": [[float(px), float(qty)] for px, qty in asks[:limit]],
        "bid_depth_top5_usdt": sum(float(px) * float(qty) for px, qty in bids[:limit]),
        "ask_depth_top5_usdt": sum(float(px) * float(qty) for px, qty in asks[:limit]),
    }


def estimate_min_contract_amount(market: Dict[str, Any], mark: float, notional_buffer: float) -> Dict[str, Any]:
    limits = market.get("limits") or {}
    amount_limits = limits.get("amount") or {}
    cost_limits = limits.get("cost") or {}
    min_amount = finite(amount_limits.get("min")) or 1.0
    min_cost = finite(cost_limits.get("min"))
    contract_size = finite(market.get("contractSize")) or 1.0
    by_cost = 0.0
    if min_cost:
        by_cost = min_cost / max(mark * contract_size, 1e-12)
    raw_amount = max(min_amount, by_cost) * float(notional_buffer)
    amount_precision = market.get("precision", {}).get("amount")
    amount = math.ceil(raw_amount)
    if amount_precision is not None:
        try:
            precision = int(amount_precision)
            if precision > 0:
                scale = 10**precision
                amount = math.ceil(raw_amount * scale) / scale
        except Exception:
            pass
    notional = amount * contract_size * mark
    return {
        "amount_contracts": amount,
        "estimated_notional_usdt": notional,
        "min_amount": min_amount,
        "min_cost": min_cost,
        "contract_size": contract_size,
        "notional_buffer": notional_buffer,
    }


def make_exchange(timeout_ms: int):
    import ccxt  # type: ignore

    api_key = env_first("GATEIO_API_KEY", "GATE_API_KEY", "CCXT_API_KEY")
    secret = env_first("GATEIO_API_SECRET", "GATE_SECRET", "CCXT_SECRET")
    password = env_first("GATEIO_API_PASSWORD", "GATE_PASSWORD", "CCXT_PASSWORD")
    cfg: Dict[str, Any] = {
        "enableRateLimit": True,
        "timeout": timeout_ms,
        "options": {"defaultType": "swap"},
    }
    if api_key and secret:
        cfg["apiKey"] = api_key
        cfg["secret"] = secret
    if password:
        cfg["password"] = password
    return ccxt.gateio(cfg)


def summarize_order(order: Dict[str, Any]) -> Dict[str, Any]:
    fields = [
        "id",
        "clientOrderId",
        "timestamp",
        "datetime",
        "symbol",
        "type",
        "side",
        "price",
        "average",
        "amount",
        "filled",
        "remaining",
        "cost",
        "status",
        "fee",
        "fees",
    ]
    return {key: order.get(key) for key in fields if key in order}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="AMD/USDT:USDT")
    ap.add_argument("--side", choices=["long", "short"], default="long")
    ap.add_argument("--out", required=True)
    ap.add_argument("--timeout-ms", type=int, default=20000)
    ap.add_argument("--notional-buffer", type=float, default=1.05)
    ap.add_argument("--close-after-sec", type=float, default=8.0)
    ap.add_argument("--no-immediate-close", action="store_true")
    ap.add_argument("--execute-live", action="store_true")
    ap.add_argument("--i-accept-real-order", action="store_true")
    args = ap.parse_args()

    live = bool(args.execute_live and args.i_accept_real_order)
    ex = make_exchange(args.timeout_ms)
    started = utc_now()
    ex.load_markets()
    if args.symbol not in ex.markets:
        raise SystemExit(f"symbol not found on gateio: {args.symbol}")
    market = ex.markets[args.symbol]
    ticker = ex.fetch_ticker(args.symbol)
    mark = finite(ticker.get("mark")) or finite(ticker.get("last")) or finite(ticker.get("close"))
    if not mark:
        raise SystemExit("missing mark/last price")
    pre_book = compact_book(ex.fetch_order_book(args.symbol, limit=5))
    size = estimate_min_contract_amount(market, mark, args.notional_buffer)
    entry_side = "buy" if args.side == "long" else "sell"
    exit_side = "sell" if args.side == "long" else "buy"

    report: Dict[str, Any] = {
        "started_at": started,
        "completed_at": None,
        "mode": "live" if live else "dry_run",
        "safety": {
            "exchange": "gateio",
            "symbol": args.symbol,
            "real_order_requested": bool(args.execute_live),
            "real_order_confirmed": bool(args.i_accept_real_order),
            "immediate_close_requested": not args.no_immediate_close,
            "secrets_printed": False,
        },
        "market": {
            "symbol": args.symbol,
            "market_id": market.get("id"),
            "type": market.get("type"),
            "active": market.get("active"),
            "maker": market.get("maker"),
            "taker": market.get("taker"),
            "mark": mark,
            "pre_orderbook": pre_book,
        },
        "size": size,
        "orders": {},
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if not live:
        report["completed_at"] = utc_now()
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps({"status": "dry_run_complete", "out": str(out), "size": size}, ensure_ascii=False))
        return

    entry_params = {"reduceOnly": False}
    close_params = {"reduceOnly": True}
    entry_order = ex.create_order(args.symbol, "market", entry_side, size["amount_contracts"], None, entry_params)
    report["orders"]["entry_create"] = summarize_order(entry_order)
    time.sleep(max(0.0, float(args.close_after_sec)))
    try:
        fetched_entry = ex.fetch_order(entry_order["id"], args.symbol)
        report["orders"]["entry_fetch"] = summarize_order(fetched_entry)
    except Exception as exc:
        report["orders"]["entry_fetch_error"] = str(exc)[:240]

    report["market"]["post_entry_orderbook"] = compact_book(ex.fetch_order_book(args.symbol, limit=5))
    if not args.no_immediate_close:
        close_order = ex.create_order(args.symbol, "market", exit_side, size["amount_contracts"], None, close_params)
        report["orders"]["close_create"] = summarize_order(close_order)
        time.sleep(3.0)
        try:
            fetched_close = ex.fetch_order(close_order["id"], args.symbol)
            report["orders"]["close_fetch"] = summarize_order(fetched_close)
        except Exception as exc:
            report["orders"]["close_fetch_error"] = str(exc)[:240]
        report["market"]["post_close_orderbook"] = compact_book(ex.fetch_order_book(args.symbol, limit=5))

    report["completed_at"] = utc_now()
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"status": "live_probe_complete", "out": str(out), "size": size}, ensure_ascii=False))


if __name__ == "__main__":
    main()
