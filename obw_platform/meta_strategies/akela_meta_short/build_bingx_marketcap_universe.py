#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import ccxt
import requests

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUT = ROOT / "_reports" / "akela_meta_short" / "bingx_marketcap_universe"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def fetch_coingecko_markets(pages: int) -> list[dict]:
    out = []
    for page in range(1, int(pages) + 1):
        url = "https://api.coingecko.com/api/v3/coins/markets"
        params = {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": 250,
            "page": page,
            "sparkline": "false",
        }
        r = requests.get(url, params=params, timeout=30)
        if r.status_code == 429 and out:
            break
        r.raise_for_status()
        chunk = r.json()
        if not chunk:
            break
        out.extend(chunk)
    return out


def fetch_coinlore_markets(limit: int = 500) -> list[dict]:
    out = []
    start = 0
    while start < limit:
        url = "https://api.coinlore.net/api/tickers/"
        r = requests.get(url, params={"start": start, "limit": min(100, limit - start)}, timeout=30)
        r.raise_for_status()
        data = r.json().get("data") or []
        if not data:
            break
        for coin in data:
            out.append({
                "id": coin.get("id"),
                "symbol": str(coin.get("symbol") or "").lower(),
                "name": coin.get("name"),
                "market_cap": float(coin.get("market_cap_usd") or 0.0),
                "market_cap_rank": int(coin.get("rank") or 0),
                "total_volume": float(coin.get("volume24") or 0.0),
                "source": "coinlore",
            })
        start += len(data)
        if len(data) < 100:
            break
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exchange", default="bingx")
    ap.add_argument("--lower-symbol", default="ENA")
    ap.add_argument("--upper-symbol", default="XRP")
    ap.add_argument("--pages", type=int, default=3)
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    ex = getattr(ccxt, args.exchange)({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    markets = ex.load_markets()
    futures = []
    for symbol, market in markets.items():
        if market.get("swap") and market.get("quote") == "USDT" and market.get("active", True):
            base = str(market.get("base") or "").upper()
            if base:
                futures.append({"base": base, "symbol": symbol})
    by_base = {}
    for row in futures:
        by_base.setdefault(row["base"], row["symbol"])

    source = "coingecko"
    try:
        cg = fetch_coingecko_markets(args.pages)
    except Exception:
        cg = fetch_coinlore_markets()
        source = "coinlore"
    cg_by_symbol = {}
    for coin in cg:
        sym = str(coin.get("symbol") or "").upper()
        if not sym:
            continue
        # Keep the largest market cap if symbols collide.
        prev = cg_by_symbol.get(sym)
        if prev is None or float(coin.get("market_cap") or 0) > float(prev.get("market_cap") or 0):
            cg_by_symbol[sym] = coin

    lower = cg_by_symbol.get(args.lower_symbol.upper())
    upper = cg_by_symbol.get(args.upper_symbol.upper())
    if not lower or not upper:
        raise SystemExit(f"missing CoinGecko anchors: lower={bool(lower)} upper={bool(upper)}")
    low_cap = float(lower.get("market_cap") or 0)
    high_cap = float(upper.get("market_cap") or 0)
    lo, hi = sorted([low_cap, high_cap])

    selected = []
    missing_marketcap = []
    for base, symbol in sorted(by_base.items()):
        coin = cg_by_symbol.get(base)
        if not coin:
            missing_marketcap.append({"base": base, "symbol": symbol})
            continue
        cap = float(coin.get("market_cap") or 0)
        if lo < cap < hi:
            selected.append({
                "base": base,
                "symbol": symbol,
                "coingecko_id": coin.get("id"),
                "name": coin.get("name"),
                "market_cap": cap,
                "market_cap_rank": coin.get("market_cap_rank"),
                "total_volume": coin.get("total_volume"),
            })
    selected.sort(key=lambda x: float(x.get("market_cap") or 0), reverse=True)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "bingx_marketcap_between_anchors_v1",
        "ts_utc": utc_now(),
        "exchange": args.exchange,
        "market_cap_source": source,
        "anchors": {
            args.lower_symbol.upper(): {"market_cap": low_cap, "coingecko_id": lower.get("id"), "rank": lower.get("market_cap_rank")},
            args.upper_symbol.upper(): {"market_cap": high_cap, "coingecko_id": upper.get("id"), "rank": upper.get("market_cap_rank")},
        },
        "range_market_cap_min": lo,
        "range_market_cap_max": hi,
        "selected_count": len(selected),
        "selected": selected,
        "missing_marketcap_count": len(missing_marketcap),
        "missing_marketcap_examples": missing_marketcap[:100],
    }
    (out_dir / "bingx_between_ena_xrp_marketcap.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    (out_dir / "universe_bingx_between_ena_xrp.txt").write_text("\n".join(row["symbol"] for row in selected) + "\n", encoding="utf-8")
    (out_dir / "universe_bingx_between_ena_xrp_bases.txt").write_text("\n".join(row["base"] for row in selected) + "\n", encoding="utf-8")
    print(json.dumps({"selected_count": len(selected), "universe": str(out_dir / "universe_bingx_between_ena_xrp.txt"), "top": selected[:20]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
