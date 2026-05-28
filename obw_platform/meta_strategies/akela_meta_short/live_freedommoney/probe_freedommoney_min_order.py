#!/usr/bin/env python3
"""Read BingX public market metadata for FREEDOMMONEY minimum order sizing.

This script does not authenticate and does not create orders.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import ccxt


ROOT = Path(__file__).resolve().parents[4]
OUT = ROOT / "_reports" / "akela_meta_short" / "freedommoney_live_prep" / "freedommoney_min_order_probe.json"
SYMBOL = "FREEDOMMONEY/USDT:USDT"


def main() -> int:
    ex = ccxt.bingx({"enableRateLimit": True})
    ex.load_markets()
    market = ex.market(SYMBOL)
    ticker = ex.fetch_ticker(SYMBOL)
    price = float(ticker.get("last") or ticker.get("bid") or ticker.get("ask") or 0.0)
    min_qty = float(((market.get("limits") or {}).get("amount") or {}).get("min") or 0.0)
    min_cost = float(((market.get("limits") or {}).get("cost") or {}).get("min") or 0.0)
    notional_from_min_qty = price * min_qty
    suggested_min_order_usdt = max(min_cost, notional_from_min_qty) * 1.08
    payload = {
        "schema": "freedommoney_min_order_probe_v1",
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "exchange": "bingx",
        "symbol": SYMBOL,
        "last_price": price,
        "market_id": market.get("id"),
        "precision": market.get("precision"),
        "limits": market.get("limits"),
        "min_qty": min_qty,
        "min_cost": min_cost,
        "notional_from_min_qty": notional_from_min_qty,
        "suggested_min_order_usdt_8pct_buffer": suggested_min_order_usdt,
        "recommended_config_minOrderUSDT": round(suggested_min_order_usdt + 0.0000001, 2),
        "creates_orders": False,
        "uses_private_api": False,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
