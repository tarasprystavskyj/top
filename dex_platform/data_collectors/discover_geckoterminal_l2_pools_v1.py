#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, math, re, sys, time
from pathlib import Path
from typing import Any, Dict, List
import pandas as pd
import requests

GECKO_BASE = "https://api.geckoterminal.com/api/v2"
HEADERS = {"accept": "application/json", "user-agent": "dex-l2-pool-discovery/1.0"}

def normalize_symbol(raw: str) -> str:
    s = str(raw).strip()
    if not s:
        return ""
    if "/" in s:
        s = s.split("/", 1)[0]
    if ":" in s:
        s = s.split(":", 1)[0]
    return s.strip().upper()

def load_symbols(args) -> List[str]:
    out = []
    if args.symbols:
        out += [normalize_symbol(x) for x in args.symbols.split(",") if normalize_symbol(x)]
    if args.universe_file:
        p = Path(args.universe_file)
        if not p.exists():
            raise SystemExit(f"universe file not found: {p}")
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            x = normalize_symbol(line)
            if x:
                out.append(x)
    seen, res = set(), []
    for x in out:
        if x not in seen:
            seen.add(x); res.append(x)
    return res

def safe_float(v: Any, default=0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default

def get_nested_float(attrs: Dict[str, Any], *keys, default=0.0) -> float:
    cur = attrs
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return safe_float(cur, default)

def gecko_get(url: str, params: Dict[str, Any], retries=5, sleep_s=2.2) -> Dict[str, Any]:
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=30)
            if r.status_code == 429:
                wait = max(10.0, sleep_s + i * 5)
                print(f"[WARN] 429 rate limit; sleep {wait}s", file=sys.stderr)
                time.sleep(wait); continue
            if r.status_code in (500, 502, 503, 504):
                wait = max(sleep_s, 2 ** i)
                print(f"[WARN] {r.status_code}; sleep {wait}s", file=sys.stderr)
                time.sleep(wait); continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            wait = max(sleep_s, 2 ** i)
            print(f"[WARN] request failed: {e}; sleep {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"GeckoTerminal request failed: {last}")

def included_lookup(doc):
    inc = {}
    for item in doc.get("included") or []:
        typ, iid = item.get("type"), item.get("id")
        if typ and iid:
            inc[f"{typ}:{iid}"] = item
    return inc

def rel_item(row, name, inc):
    rel = ((row.get("relationships") or {}).get(name) or {}).get("data")
    if isinstance(rel, list):
        rel = rel[0] if rel else None
    if not isinstance(rel, dict):
        return {}
    return inc.get(f"{rel.get('type')}:{rel.get('id')}", {})

def flatten_pool(symbol, network, item, inc):
    attrs = item.get("attributes") or {}
    dex = rel_item(item, "dex", inc)
    base_token = rel_item(item, "base_token", inc)
    quote_token = rel_item(item, "quote_token", inc)
    dex_attrs = dex.get("attributes") or {}
    base_attrs = base_token.get("attributes") or {}
    quote_attrs = quote_token.get("attributes") or {}

    pool_id = item.get("id", "")
    address = attrs.get("address") or pool_id.split("_")[-1]
    vol24 = get_nested_float(attrs, "volume_usd", "h24")
    tvl = safe_float(attrs.get("reserve_in_usd"))
    tx24 = get_nested_float(attrs, "transactions", "h24", "buys") + get_nested_float(attrs, "transactions", "h24", "sells")
    vol_tvl = vol24 / tvl if tvl > 0 else 0.0
    dex_id = dex.get("id") or ""

    return {
        "query_symbol": symbol,
        "network": network,
        "pool_id": pool_id,
        "pool_address": str(address).lower(),
        "pool_name": attrs.get("name"),
        "dex_id": dex_id,
        "dex_name": dex_attrs.get("name"),
        "base_token_symbol": base_attrs.get("symbol"),
        "base_token_name": base_attrs.get("name"),
        "base_token_address": str(base_attrs.get("address") or "").lower(),
        "quote_token_symbol": quote_attrs.get("symbol"),
        "quote_token_name": quote_attrs.get("name"),
        "quote_token_address": str(quote_attrs.get("address") or "").lower(),
        "price_usd": safe_float(attrs.get("base_token_price_usd")),
        "tvl_usd": tvl,
        "volume_h24_usd": vol24,
        "volume_h6_usd": get_nested_float(attrs, "volume_usd", "h6"),
        "volume_h1_usd": get_nested_float(attrs, "volume_usd", "h1"),
        "volume_tvl_h24": vol_tvl,
        "tx_h24": tx24,
        "fdv_usd": safe_float(attrs.get("fdv_usd")),
        "market_cap_usd": safe_float(attrs.get("market_cap_usd")),
        "raw_api_url": f"{GECKO_BASE}/networks/{network}/pools/{address}",
    }

def score_row(r):
    tvl = max(float(r.get("tvl_usd") or 0), 0)
    vol = max(float(r.get("volume_h24_usd") or 0), 0)
    tx = max(float(r.get("tx_h24") or 0), 0)
    vt = max(float(r.get("volume_tvl_h24") or 0), 0)
    return math.log1p(tvl) + 1.8 * math.log1p(vol) + 2.5 * min(vt, 5.0) + 0.25 * math.log1p(tx)

def search_symbol_network(symbol, network, sleep_s):
    url = f"{GECKO_BASE}/search/pools"
    params = {"query": symbol, "network": network, "include": "base_token,quote_token,dex,network"}
    doc = gecko_get(url, params, sleep_s=sleep_s)
    inc = included_lookup(doc)
    return [flatten_pool(symbol, network, item, inc) for item in (doc.get("data") or [])]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="")
    ap.add_argument("--universe-file", default="")
    ap.add_argument("--networks", default="base,arbitrum,optimism")
    ap.add_argument("--out-csv", required=True)
    ap.add_argument("--out-json", default="")
    ap.add_argument("--min-tvl-usd", type=float, default=10000)
    ap.add_argument("--min-volume-h24-usd", type=float, default=5000)
    ap.add_argument("--prefer-dex-regex", default="uniswap|aerodrome|velodrome|pancake")
    ap.add_argument("--only-preferred-dex", action="store_true")
    ap.add_argument("--sleep-s", type=float, default=2.2)
    ap.add_argument("--limit-symbols", type=int, default=0)
    args = ap.parse_args()

    symbols = load_symbols(args)
    if args.limit_symbols > 0:
        symbols = symbols[:args.limit_symbols]
    if not symbols:
        raise SystemExit("No symbols. Use --symbols or --universe-file.")

    networks = [x.strip() for x in args.networks.split(",") if x.strip()]
    rx = re.compile(args.prefer_dex_regex, re.I)
    rows = []
    for i, sym in enumerate(symbols, 1):
        for network in networks:
            print(f"[search] {i}/{len(symbols)} {sym} {network}", file=sys.stderr)
            try:
                for r in search_symbol_network(sym, network, args.sleep_s):
                    text = f"{r.get('dex_id')} {r.get('dex_name')}"
                    r["dex_preferred"] = bool(rx.search(text))
                    r["score"] = score_row(r)
                    rows.append(r)
            except Exception as e:
                print(f"[ERROR] {sym} {network}: {e}", file=sys.stderr)
            time.sleep(args.sleep_s)

    df = pd.DataFrame(rows)
    out_csv = Path(args.out_csv); out_csv.parent.mkdir(parents=True, exist_ok=True)
    if not df.empty:
        df = df[(pd.to_numeric(df["tvl_usd"], errors="coerce").fillna(0) >= args.min_tvl_usd) &
                (pd.to_numeric(df["volume_h24_usd"], errors="coerce").fillna(0) >= args.min_volume_h24_usd)].copy()
        if args.only_preferred_dex:
            df = df[df["dex_preferred"] == True].copy()
        df = df.sort_values(["dex_preferred", "score", "volume_tvl_h24", "volume_h24_usd"],
                            ascending=[False, False, False, False]).reset_index(drop=True)
    df.to_csv(out_csv, index=False)
    print(f"Wrote {len(df)} rows: {out_csv}")

    if args.out_json:
        out_json = Path(args.out_json); out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(df.to_json(orient="records", force_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote JSON: {out_json}")

    if not df.empty:
        cols = ["query_symbol","network","dex_name","pool_name","pool_address","tvl_usd","volume_h24_usd","volume_tvl_h24","tx_h24","score"]
        print(df[cols].head(30).to_string(index=False))

if __name__ == "__main__":
    main()
