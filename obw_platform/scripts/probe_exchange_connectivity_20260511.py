#!/usr/bin/env python3
"""Small ccxt connectivity probe for market-data endpoints."""

import json
import time

import ccxt


def main():
    out = {}
    for exid in ("bingx", "bybit", "gateio"):
        t0 = time.time()
        try:
            ex = getattr(ccxt, exid)({"enableRateLimit": True})
            ex.load_markets()
            out[exid] = {
                "ok": True,
                "markets": len(getattr(ex, "markets", {}) or {}),
                "elapsed_sec": round(time.time() - t0, 3),
            }
        except Exception as e:
            out[exid] = {
                "ok": False,
                "error_type": type(e).__name__,
                "error": str(e).splitlines()[0][:500],
                "elapsed_sec": round(time.time() - t0, 3),
            }
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
