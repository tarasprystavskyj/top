
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Build BTC<->ALTs flow series from an existing OHLCV/indicators cache (price_indicators table).
# - Reads your DB created by fetch_build_cache_v15.py (or v16).
# - Detects available BTC cross pairs (ALT/BTC or BTC/ALT) already present in the DB.
# - For each pair and each bar, computes a heuristic "net flow" and a "gross volume" in BTC & USD:
#     * gross_btc:
#         - if ALT/BTC: use quote_volume (already in BTC units)
#         - if BTC/ALT: use volume (base volume, in BTC units)
#     * btc_usd at that bar is taken from BTC/USDT (or BTC/USDC) close price
#     * gross_usd = gross_btc * btc_usd
#     * direction (dir): +1 means BTC->ALT, -1 means ALT->BTC
#         - if ALT/BTC and return (close/open) > 1 => dir = +1
#         - if BTC/ALT and return (close/open) > 1 => dir = -1   (opposite, since base is BTC)
#   (This is a price-action proxy when per-trade buy/sell breakdowns are unavailable.)
# - Writes results into a new table: btc_pair_flows.
# - Optionally exports a compact JSON for the 3D visual (priceSeries + flowSeries).
#
# Limitations:
# - Uses OHLCV to infer direction; for higher fidelity, aggregate real trades by side.
# - Requires BTC/USDT (or BTC/USDC) series in the same DB for USD conversion.
#
# Usage examples:
#   python3 build_btc_flows.py --db combined_cache_5m.db --top-n 10 --export-json flows_5m.json
#   python3 build_btc_flows.py --db combined_cache_1h.db --alts ETH,SOL,LTC,LINK
#
# Author: chatgpt

import argparse, sqlite3, json, math
from collections import defaultdict
from typing import Dict, List, Tuple, Optional
import pandas as pd
import numpy as np

# --- symbol parsing helpers ---
def norm(s: str) -> str:
    return str(s or "").strip().upper()

def parse_base_quote(market: str) -> Tuple[str, str]:
    """
    Accepts CCXT market ids like: ETH/USDT, BTC/USDT:USDT, ETH/BTC, BTC/ETH
    Returns (base, quote) stripped of CCXT suffix after colon (e.g., :USDT)
    """
    m = norm(market)
    if "/" not in m:  # fallback
        return m, ""
    base, quote = m.split("/", 1)
    quote = quote.split(":")[0]  # remove :USDT etc
    return base, quote

def is_btc_cross(market: str) -> bool:
    b,q = parse_base_quote(market)
    return b == "BTC" or q == "BTC"

def is_usd_quote(market: str) -> bool:
    b,q = parse_base_quote(market)
    return q in {"USDT","USDC"}

# --- db helpers ---
def read_distinct_symbols(con: sqlite3.Connection) -> List[str]:
    cur = con.cursor()
    cur.execute("SELECT DISTINCT symbol FROM price_indicators")
    return [r[0] for r in cur.fetchall()]

def read_series(con: sqlite3.Connection, market: str, cols=("open","close","volume","quote_volume")) -> pd.DataFrame:
    qcols = ",".join(["datetime_utc"] + list(cols))
    df = pd.read_sql_query(
        f"SELECT {qcols} FROM price_indicators WHERE symbol = ? ORDER BY datetime_utc ASC",
        con, params=(market,)
    )
    if df.empty:
        return df
    df = df.set_index("datetime_utc")
    return df

def ensure_flows_schema(con: sqlite3.Connection) -> None:
    cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS btc_pair_flows (
        datetime_utc TEXT NOT NULL,
        base TEXT NOT NULL,            -- 'BTC'
        alt  TEXT NOT NULL,            -- e.g. 'ETH'
        pair_symbol TEXT NOT NULL,     -- e.g. 'ETH/BTC' or 'BTC/ETH'
        gross_btc REAL NOT NULL,
        btc_usd REAL NOT NULL,
        gross_usd REAL NOT NULL,
        ret REAL NOT NULL,             -- close/open - 1 (for that market orientation)
        dir INTEGER NOT NULL,          -- +1: BTC->ALT, -1: ALT->BTC
        net_flow_usd REAL NOT NULL,    -- signed by dir
        thickness REAL,                -- 0..1 normalized magnitude (filled at end)
        PRIMARY KEY (datetime_utc, pair_symbol)
    )""")
    # convenient indexes
    cur.execute("CREATE INDEX IF NOT EXISTS idx_flows_dt ON btc_pair_flows(datetime_utc)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_flows_alt ON btc_pair_flows(alt)")
    con.commit()

# --- main build ---
def build_flows(db_path: str, alts: Optional[List[str]], top_n: Optional[int], export_json: Optional[str]) -> None:
    con = sqlite3.connect(db_path)
    ensure_flows_schema(con)

    symbols = read_distinct_symbols(con)
    if not symbols:
        raise SystemExit("No symbols in price_indicators. Did you run fetch_build_cache first?")

    # BTC USD price series (prefer USDT, fallback USDC)
    btc_usd_mkt = next((s for s in symbols if norm(s).startswith("BTC/USDT")), None)
    if not btc_usd_mkt:
        btc_usd_mkt = next((s for s in symbols if norm(s).startswith("BTC/USDC")), None)
    if not btc_usd_mkt:
        raise SystemExit("BTC/USDT or BTC/USDC series required for USD conversion.")

    btc_usd = read_series(con, btc_usd_mkt, cols=("close",))
    btc_usd.columns = ["btc_usd"]
    if btc_usd.empty:
        raise SystemExit("Empty BTC/USD price series.")

    # Candidate BTC crosses
    btc_crosses = [s for s in symbols if is_btc_cross(s)]
    # Exclude stable quotes (they are used only for conversion and not as ALTs)
    btc_crosses = [s for s in btc_crosses if not is_usd_quote(s)]
    # Map ALT -> best BTC cross symbol present (prefer ALT/BTC, fallback BTC/ALT)
    alt2market: Dict[str, str] = {}
    for s in btc_crosses:
        b,q = parse_base_quote(s)
        if b == "BTC":
            alt = q
            # prefer ALT/BTC if exists; else keep BTC/ALT
            if alt not in alt2market:  # set if none
                alt2market[alt] = s
        else: # q == BTC
            alt = b
            # always prefer ALT/BTC; if we already have it, skip
            cur = alt2market.get(alt)
            if (cur is None) or parse_base_quote(cur)[0] == "BTC":
                alt2market[alt] = s

    # Filter by requested ALTs / top N
    if alts:
        alts = [norm(a) for a in alts if norm(a) in alt2market]
    else:
        alts = sorted(alt2market.keys())

    if top_n is not None and top_n > 0 and len(alts) > top_n:
        # pick top by average gross volume proxy across available series
        vols = []
        for alt in alts:
            mkt = alt2market[alt]
            df = read_series(con, mkt, cols=("volume","quote_volume","open","close"))
            if df.empty: continue
            b,q = parse_base_quote(mkt)
            if q == "BTC":
                gross_btc = df["quote_volume"]
            else: # base is BTC
                gross_btc = df["volume"]
            vols.append((alt, float(np.nanmean(gross_btc.values))))
        alts = [a for a,_ in sorted(vols, key=lambda x: x[1], reverse=True)[:top_n]]

    # Build flows rows
    all_rows = []
    for alt in alts:
        mkt = alt2market[alt]
        df = read_series(con, mkt, cols=("open","close","volume","quote_volume"))
        if df.empty: 
            continue
        pair = parse_base_quote(mkt)  # (base, quote)
        # join with btc_usd to ensure alignment
        tmp = df.join(btc_usd, how="inner")
        if tmp.empty:
            continue

        # compute gross_btc and dir sign
        if pair[1] == "BTC":  # ALT/BTC
            gross_btc = tmp["quote_volume"]  # quote is BTC
            ret = (tmp["close"] / tmp["open"] - 1.0)
            dir_sign = np.where(ret > 0, 1, -1)  # +1 BTC->ALT when ALT strengthens
        else:  # BTC/ALT
            gross_btc = tmp["volume"]        # base is BTC
            ret = (tmp["close"] / tmp["open"] - 1.0)
            dir_sign = np.where(ret > 0, -1, 1) # + ret => ALT->BTC (dir -1)

        gross_usd = gross_btc * tmp["btc_usd"]
        net_flow_usd = gross_usd * dir_sign

        rows = pd.DataFrame({
            "datetime_utc": tmp.index,
            "base": ["BTC"]*len(tmp),
            "alt": [alt]*len(tmp),
            "pair_symbol": [mkt]*len(tmp),
            "gross_btc": gross_btc.values.astype(float),
            "btc_usd": tmp["btc_usd"].values.astype(float),
            "gross_usd": gross_usd.values.astype(float),
            "ret": ret.values.astype(float),
            "dir": dir_sign.astype(int),
            "net_flow_usd": net_flow_usd.values.astype(float),
        })

        all_rows.append(rows)

    if not all_rows:
        raise SystemExit("No ALT/BTC or BTC/ALT pairs found for selected alts.")

    flows = pd.concat(all_rows, ignore_index=True)
    # Normalize thickness 0..1 across all pairs by 95th percentile of |gross_usd|
    p95 = float(np.percentile(np.abs(flows["gross_usd"].values), 95))
    if p95 <= 0: p95 = float(np.max(np.abs(flows["gross_usd"].values)) or 1.0)
    flows["thickness"] = np.clip(np.abs(flows["gross_usd"]) / p95, 0, 1)

    # Upsert into DB
    cur = con.cursor()
    cur.execute("BEGIN")
    cur.execute("DELETE FROM btc_pair_flows")  # rebuild fully for simplicity
    cur.executemany(
        """INSERT OR REPLACE INTO btc_pair_flows
           (datetime_utc, base, alt, pair_symbol, gross_btc, btc_usd, gross_usd, ret, dir, net_flow_usd, thickness)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        [tuple(r) for r in flows[["datetime_utc","base","alt","pair_symbol","gross_btc","btc_usd","gross_usd","ret","dir","net_flow_usd","thickness"]].itertuples(index=False, name=None)]
    )
    con.commit()

    # Optional export for front-end (compact structure expected by the 3D demo)
    if export_json:
        # Build priceSeries for BTC + ALTs in USD
        price_series: Dict[str, Dict[str, float]] = {}
        # BTC price
        price_series["BTC"] = {dt: float(px) for dt, px in btc_usd["btc_usd"].items()}

        # ALT USD price via ALT/USDT if available; else via (ALT/BTC * BTCUSD) or inverted (BTC/ALT) if needed
        symbols = read_distinct_symbols(con)  # refresh
        alt_usd_map = {norm(s).split("/")[0]: s for s in symbols if norm(s).endswith("/USDT") or norm(s).endswith("/USDT:USDT")}
        alts_used = alts
        for alt in alts_used:
            mkt = alt_usd_map.get(alt)
            if mkt:
                sdf = read_series(con, mkt, cols=("close",)).rename(columns={"close":"alt_usd"})
                merged = sdf.join(btc_usd, how="inner")
                price_series[alt] = {dt: float(mx["alt_usd"]) for dt, mx in merged.iterrows()}
            else:
                # derive from BTC cross used
                cross = mkt = mkt or ""  # to avoid linter
                cross = alt2market.get(alt) if 'alt2market' in locals() else None
                if not cross:
                    continue
                sdf = read_series(con, cross, cols=("close",))
                sdf = sdf.join(btc_usd, how="inner")
                base, quote = parse_base_quote(cross)
                if quote == "BTC":  # ALT/BTC => alt_usd = (ALT/BTC)*BTCUSD
                    alt_usd = sdf["close"] * sdf["btc_usd"]
                else:               # BTC/ALT => alt_usd = BTCUSD / (BTC/ALT)
                    alt_usd = sdf["btc_usd"] / np.where(sdf["close"]>0, sdf["close"], np.nan)
                price_series[alt] = {dt: (float(px) if not (px is np.nan) else None) for dt, px in alt_usd.items()}

        # Flow series compact: for each pair BTC<->ALT aligned to BTC timeline
        frames = defaultdict(list)
        times = sorted(set(flows["datetime_utc"].tolist()))
        fl_by_pair = {}
        for alt in alts_used:
            sub = flows[flows["alt"]==alt].set_index("datetime_utc")
            fl_by_pair[f"BTC_{alt}"] = sub[["gross_usd","dir","thickness"]]

        flow_series = {}
        for key, sub in fl_by_pair.items():
            ser = []
            for dt in times:
                if dt in sub.index:
                    row = sub.loc[dt]
                    ser.append({"vol": float(abs(row["gross_usd"])), "dir": int(row["dir"])})
                else:
                    ser.append({"vol": 0.0, "dir": 0})
            flow_series[key] = ser

        payload = {
            "times": times,
            "nodes": [{"id":"BTC"}] + [{"id":a} for a in alts_used],
            "priceSeries": price_series,
            "flowSeries": flow_series
        }
        with open(export_json, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        print(f"[OK] Exported {export_json}")

    con.close()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="Path to SQLite built by fetch_build_cache_v15/v16 (price_indicators table)")
    ap.add_argument("--alts", default="", help="Comma-separated list of alts (e.g., ETH,SOL,LTC). If empty, auto-detect.")
    ap.add_argument("--top-n", type=int, default=10, help="Take top-N alts by average BTC cross volume when --alts not provided")
    ap.add_argument("--export-json", default="", help="Optional: path to save compact JSON for the 3D visual")
    args = ap.parse_args()

    alts = [a.strip() for a in args.alts.split(",") if a.strip()] if args.alts else None
    top_n = args.top_n if (alts is None) else None
    export_json = args.export_json if args.export_json else None

    build_flows(args.db, alts, top_n, export_json)

if __name__ == "__main__":
    main()
