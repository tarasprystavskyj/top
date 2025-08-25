
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Simple FastAPI to serve BTC flow data for the 3D visualization.
#
# Endpoints:
#   GET /pairs                 -> list of available BTC<->ALT pairs
#   GET /series?alts=ETH,SOL   -> compact JSON {times, nodes, priceSeries, flowSeries} for requested ALTs
#   GET /frame?ts=YYYY-mm-ddTHH:MM:SS+00:00&alts=ETH,SOL
#                               -> single frame {ts, nodes, links} for direct rendering
#
# Run:
#   uvicorn flows_api:app --host 0.0.0.0 --port 8000

import os, sqlite3, json
from typing import List, Dict
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import pandas as pd

DB_PATH = os.getenv("FLOWS_DB", "combined_cache.db")  # same DB containing btc_pair_flows + price_indicators

app = FastAPI(title="BTC Flow API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def conn():
    if not os.path.exists(DB_PATH):
        raise RuntimeError(f"DB not found: {DB_PATH}")
    return sqlite3.connect(DB_PATH)

@app.get("/pairs")
def list_pairs():
    with conn() as con:
        df = pd.read_sql_query("SELECT DISTINCT alt, pair_symbol FROM btc_pair_flows ORDER BY alt", con)
    if df.empty:
        return []
    out = [{"alt": r["alt"], "pair": r["pair_symbol"]} for _, r in df.iterrows()]
    return out

@app.get("/series")
def get_series(alts: str = ""):
    req = [a.strip().upper() for a in alts.split(",") if a.strip()] if alts else []
    with conn() as con:
        # timestamps
        ts = pd.read_sql_query("SELECT DISTINCT datetime_utc FROM btc_pair_flows ORDER BY datetime_utc", con)["datetime_utc"].tolist()
        # BTC price
        btc = pd.read_sql_query("SELECT datetime_utc, close FROM price_indicators WHERE symbol LIKE 'BTC/USDT%' ORDER BY datetime_utc", con)
        if btc.empty:
            raise HTTPException(400, "BTC/USDT series not found")
        btc = btc.set_index("datetime_utc")["close"]
        price_series = {"BTC": btc.to_dict()}

        # available ALTs
        avail = pd.read_sql_query("SELECT DISTINCT alt FROM btc_pair_flows ORDER BY alt", con)["alt"].tolist()
        if req:
            alts_keep = [a for a in req if a in avail]
        else:
            alts_keep = avail

        # price series for ALTs (USD)
        # try ALT/USDT else derive from BTC cross
        for alt in alts_keep:
            # direct ALT/USDT
            s = pd.read_sql_query(
                "SELECT datetime_utc, close FROM price_indicators WHERE symbol = ? ORDER BY datetime_utc",
                con, params=(f"{alt}/USDT",)
            )
            if not s.empty:
                price_series[alt] = s.set_index("datetime_utc")["close"].to_dict()
            else:
                # derive from used BTC cross
                m = pd.read_sql_query(
                    "SELECT pair_symbol FROM btc_pair_flows WHERE alt = ? LIMIT 1", con, params=(alt,)
                )
                if m.empty:
                    continue
                pair = m.iloc[0,0]
                sdf = pd.read_sql_query(
                    "SELECT datetime_utc, close FROM price_indicators WHERE symbol = ? ORDER BY datetime_utc",
                    con, params=(pair,)
                ).set_index("datetime_utc")["close"]
                merged = pd.DataFrame({"alt_btc_or_inv": sdf, "btc_usd": btc}).dropna()
                if "/BTC" in pair:
                    alt_usd = merged["alt_btc_or_inv"] * merged["btc_usd"]
                else:
                    # BTC/ALT
                    alt_usd = merged["btc_usd"] / merged["alt_btc_or_inv"].replace({0: None})
                price_series[alt] = alt_usd.to_dict()

        # flows
        flow_series: Dict[str, List[Dict]] = {}
        for alt in alts_keep:
            df = pd.read_sql_query(
                "SELECT datetime_utc, gross_usd, dir FROM btc_pair_flows WHERE alt = ? ORDER BY datetime_utc",
                con, params=(alt,)
            )
            sub = {r["datetime_utc"]: {"vol": float(abs(r["gross_usd"])), "dir": int(r["dir"])} for _, r in df.iterrows()}
            flow_series[f"BTC_{alt}"] = [sub.get(t, {"vol": 0.0, "dir": 0}) for t in ts]

    payload = {
        "times": ts,
        "nodes": [{"id": "BTC"}] + [{"id": a} for a in alts_keep],
        "priceSeries": price_series,
        "flowSeries": flow_series
    }
    return payload

@app.get("/frame")
def get_frame(ts: str, alts: str = ""):
    req = [a.strip().upper() for a in alts.split(",") if a.strip()] if alts else []
    with conn() as con:
        # BTC price
        btc = pd.read_sql_query(
            "SELECT close FROM price_indicators WHERE symbol LIKE 'BTC/USDT%' AND datetime_utc = ?",
            con, params=(ts,)
        )
        if btc.empty:
            raise HTTPException(404, f"No BTC quote at {ts}")
        btc_usd = float(btc.iloc[0,0])

        avail = pd.read_sql_query("SELECT DISTINCT alt FROM btc_pair_flows", con)["alt"].tolist()
        alts_keep = [a for a in (req if req else avail) if a in avail]

        # nodes (with per-frame price if available)
        nodes = [{"id":"BTC", "price": btc_usd}]
        for alt in alts_keep:
            row = pd.read_sql_query(
                "SELECT close FROM price_indicators WHERE symbol = ? AND datetime_utc = ?",
                con, params=(f"{alt}/USDT", ts)
            )
            if not row.empty:
                price = float(row.iloc[0,0])
            else:
                # derive from flow pair
                pair_df = pd.read_sql_query("SELECT pair_symbol FROM btc_pair_flows WHERE alt = ? LIMIT 1", con, params=(alt,))
                if pair_df.empty:
                    price = None
                else:
                    pair = pair_df.iloc[0,0]
                    pr = pd.read_sql_query(
                        "SELECT close FROM price_indicators WHERE symbol = ? AND datetime_utc = ?",
                        con, params=(pair, ts)
                    )
                    if pr.empty:
                        price = None
                    else:
                        v = float(pr.iloc[0,0])
                        price = (v*btc_usd) if "/BTC" in pair else (btc_usd / v if v else None)
            nodes.append({"id": alt, "price": price})

        # links
        links = []
        for alt in alts_keep:
            row = pd.read_sql_query(
                "SELECT gross_usd, dir FROM btc_pair_flows WHERE alt = ? AND datetime_utc = ?",
                con, params=(alt, ts)
            )
            if row.empty:
                continue
            gross_usd, dirv = float(row.iloc[0,0]), int(row.iloc[0,1])
            links.append({
                "source": "BTC" if dirv>0 else alt,
                "target": alt if dirv>0 else "BTC",
                "vol": abs(gross_usd),
                "dir": dirv
            })

    return {"ts": ts, "nodes": nodes, "links": links}
