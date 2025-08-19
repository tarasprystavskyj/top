#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cs_rs_c2_v2_data.py — data utils & reconciliation
- Reconcile bot trades (cs_rs_c2_v2_bot.py) vs backtester trades.csv
- Compute equity similarity (target ≥ 0.98)
"""
import os, argparse, json
import pandas as pd

def read_bot(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["open_time_utc"] = pd.to_datetime(df["open_time_utc"], utc=True, errors="coerce")
    df["exit_time_utc"] = pd.to_datetime(df["exit_time_utc"], utc=True, errors="coerce")
    return df

def read_bt(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    # Normalize column names
    if "open_time_utc" not in df.columns and "open_time" in df.columns:
        df = df.rename(columns={"open_time":"open_time_utc"})
    df["open_time_utc"] = pd.to_datetime(df["open_time_utc"], utc=True, errors="coerce")
    if "exit_time_utc" in df.columns:
        df["exit_time_utc"] = pd.to_datetime(df["exit_time_utc"], utc=True, errors="coerce")
    return df

def equity_series_bot(df: pd.DataFrame) -> pd.Series:
    closed = df.dropna(subset=["exit_price"]).sort_values("exit_time_utc")
    eq = closed["realized_pnl"].cumsum() + 200.0
    eq.index = closed["exit_time_utc"].values
    return eq

def equity_series_bt(df: pd.DataFrame) -> pd.Series:
    if "equity_after" in df.columns:
        ser = df.sort_values("open_time_utc")["equity_after"]
        ser.index = df.sort_values("open_time_utc")["open_time_utc"].values
        return ser
    # Fallback: compute from realized_pnl if present
    if "realized_pnl" in df.columns:
        closed = df.dropna(subset=["exit_time_utc"]).sort_values("exit_time_utc")
        ser = closed["realized_pnl"].cumsum() + 200.0
        ser.index = closed["exit_time_utc"].values
        return ser
    return pd.Series(dtype=float)

def similarity(eq_a: pd.Series, eq_b: pd.Series) -> float:
    idx = eq_a.index.union(eq_b.index)
    a = eq_a.reindex(idx).interpolate().ffill().bfill().fillna(0.0)
    b = eq_b.reindex(idx).interpolate().ffill().bfill().fillna(0.0)
    if len(a)==0 or len(b)==0: return 0.0
    mse = ((a-b)**2).mean(); denom = b.var()+1e-9
    return float(max(0.0, 1.0 - mse/denom))

def reconcile(bot_csv: str, bt_csv: str, out_dir: str) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    bot = read_bot(bot_csv); bt = read_bt(bt_csv)
    # rough match table by (symbol, hour)
    bot_k = bot.assign(k_ts=bot["open_time_utc"].dt.floor("H"))[["symbol","k_ts","entry_price","exit_price"]]
    bt_k  = bt.assign(k_ts=bt["open_time_utc"].dt.floor("H"))[["symbol","k_ts","entry_price" if "entry_price" in bt.columns else "entry","exit_price" if "exit_price" in bt.columns else "exit"]]
    bt_k = bt_k.rename(columns={"entry":"entry_price","exit":"exit_price"})
    merged = bot_k.merge(bt_k, on=["symbol","k_ts"], how="outer", suffixes=("_bot","_bt"))
    merged.to_csv(os.path.join(out_dir,"recon.csv"), index=False)

    eq_bot = equity_series_bot(bot); eq_bt = equity_series_bt(bt)
    score = similarity(eq_bot, eq_bt)
    with open(os.path.join(out_dir,"recon_metrics.json"),"w",encoding="utf-8") as f:
        json.dump({"equity_similarity": score}, f, indent=2)
    return {"equity_similarity": score}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot-trades", required=True)
    ap.add_argument("--bt-trades", required=True)
    ap.add_argument("--out-dir", default="./recon")
    a = ap.parse_args()
    m = reconcile(a.bot_trades, a.bt_trades, a.out_dir)
    print(f"equity_similarity={m['equity_similarity']:.4f}")

if __name__ == "__main__":
    main()
