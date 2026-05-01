#!/usr/bin/env python3
from __future__ import annotations
import argparse, datetime as dt, sys, time
from pathlib import Path
from typing import Any, Dict, List
import pandas as pd
import requests

GECKO_BASE = "https://api.geckoterminal.com/api/v2"
HEADERS = {"accept": "application/json", "user-agent": "dex-gecko-ohlcv/1.0"}

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

def gecko_get(url: str, params: Dict[str, Any], retries=5, sleep_s=2.2) -> Dict[str, Any]:
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=30)
            if r.status_code == 429:
                wait = max(10.0, sleep_s + i * 5)
                print(f"[WARN] 429; sleep {wait}s", file=sys.stderr)
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
            print(f"[WARN] failed: {e}; sleep {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"request failed: {last}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--network", required=True)
    ap.add_argument("--pool", required=True)
    ap.add_argument("--timeframe", default="hour", choices=["minute","hour","day"])
    ap.add_argument("--aggregate", type=int, default=1)
    ap.add_argument("--time-from", required=True)
    ap.add_argument("--time-to", required=True)
    ap.add_argument("--out-csv", required=True)
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--currency", default="usd")
    ap.add_argument("--token", default="base", choices=["base","quote"])
    ap.add_argument("--sleep-s", type=float, default=2.2)
    args = ap.parse_args()

    start_ts, end_ts = iso_to_epoch_s(args.time_from), iso_to_epoch_s(args.time_to)
    url = f"{GECKO_BASE}/networks/{args.network}/pools/{args.pool}/ohlcv/{args.timeframe}"
    before = end_ts
    rows: List[List[Any]] = []
    while True:
        params = {"aggregate": args.aggregate, "before_timestamp": before, "limit": args.limit,
                  "currency": args.currency, "token": args.token}
        doc = gecko_get(url, params, sleep_s=args.sleep_s)
        arr = (((doc.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or [])
        if not arr:
            break
        rows.extend(arr)
        min_ts = min(int(x[0]) for x in arr)
        max_ts = max(int(x[0]) for x in arr)
        print(f"[page] rows_total={len(rows)} page_from={min_ts} page_to={max_ts}", file=sys.stderr)
        if min_ts <= start_ts:
            break
        before = min_ts - 1
        time.sleep(args.sleep_s)

    df = pd.DataFrame(rows, columns=["timestamp","open","high","low","close","volume"]) if rows else pd.DataFrame(columns=["timestamp","open","high","low","close","volume"])
    if not df.empty:
        for c in ["timestamp","open","high","low","close","volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df[(df["timestamp"] >= start_ts) & (df["timestamp"] <= end_ts)].drop_duplicates("timestamp").sort_values("timestamp")
        df["datetime_utc"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    out = Path(args.out_csv); out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    print(f"Wrote {len(df)} rows: {out}")

if __name__ == "__main__":
    main()
