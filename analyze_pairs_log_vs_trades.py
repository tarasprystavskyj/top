
#!/usr/bin/env python3
import sys, os
import pandas as pd
from datetime import datetime, timezone
import numpy as np
import matplotlib.pyplot as plt

def parse_ts(s):
    try:
        return pd.to_datetime(s, utc=True)
    except Exception:
        return pd.NaT

def main(pairs_csv="pairs_bar_log.csv", trades_csv="trades.csv", out_prefix="pairs_vs_actual"):
    if not os.path.exists(pairs_csv):
        print(f"ERR: {pairs_csv} not found"); return 1
    if not os.path.exists(trades_csv):
        print(f"ERR: {trades_csv} not found"); return 1

    pairs = pd.read_csv(pairs_csv)
    trades = pd.read_csv(trades_csv)

    # Timestamps
    pairs["ts_close_iso"] = pairs["ts_close_iso"].apply(parse_ts)
    trades["ts"] = trades["ts"].apply(parse_ts)

    # Only OPEN trades
    trades_open = trades[trades["side"].str.upper()=="OPEN"].copy()
    # Round to hour for coarse matching
    pairs["bar_hour"] = pairs["ts_close_iso"].dt.floor("H")
    trades_open["bar_hour"] = trades_open["ts"].dt.floor("H")
    # Keep only pairs rows that suggest an open
    pairs_open = pairs[pairs["pairs_signal"].isin(["OPEN_LONG","OPEN_SHORT"])].copy()
    pairs_open.rename(columns={"symbol":"symbol_pairs"}, inplace=True)

    # Prepare for matching by (symbol, hour)
    # We'll also allow +/- 1h tolerance if exact hour doesn't match
    pairs_key = pairs_open[["symbol_pairs","bar_hour","pairs_signal","z","ratio","atr_ratio","dp6h","dp12h","breadth","universe_n","top_n","in_cands"]].copy()
    trades_key = trades_open[["symbol","bar_hour","entry_price","notional"]].copy()

    # Exact join first
    m0 = pairs_key.merge(trades_key, left_on=["symbol_pairs","bar_hour"], right_on=["symbol","bar_hour"], how="left", indicator=True)
    # Try +1h and -1h for missed
    missed = m0[m0["_merge"]=="left_only"].copy()
    if not missed.empty:
        plus = pairs_key.copy(); plus["bar_hour"] = plus["bar_hour"] + pd.Timedelta(hours=1)
        minus = pairs_key.copy(); minus["bar_hour"] = minus["bar_hour"] - pd.Timedelta(hours=1)
        alt = pd.concat([plus, minus], ignore_index=True)
        m1 = missed.drop(columns=["_merge"]).merge(trades_key, left_on=["symbol_pairs","bar_hour"], right_on=["symbol","bar_hour"], how="left", indicator=True)
        # fill matches from alt
        m0.loc[m0["_merge"]=="left_only","_merge"] = m1["_merge"].values if len(m1)==len(missed) else m0.loc[m0["_merge"]=="left_only","_merge"]

    # Metrics
    expected_opens = len(pairs_open)
    actual_opens = len(trades_open)
    matched_expected = (m0["_merge"]=="both").sum()
    missed_expected = (m0["_merge"]!="both").sum()
    # Extra actuals: trades that have no expected signal that hour/symbol
    extra_actuals = trades_key.merge(pairs_key.rename(columns={"symbol_pairs":"symbol"}), on=["symbol","bar_hour"], how="left", indicator=True)
    extra_count = (extra_actuals["_merge"]=="left_only").sum()

    summary = {
        "expected_opens": expected_opens,
        "actual_opens": actual_opens,
        "matched_expected": int(matched_expected),
        "missed_expected": int(missed_expected),
        "extra_actuals": int(extra_count),
        "match_rate_%": (matched_expected/expected_opens*100.0 if expected_opens>0 else np.nan),
    }
    summ_df = pd.DataFrame([summary])
    out_csv = f"{out_prefix}_summary.csv"
    summ_df.to_csv(out_csv, index=False)

    # Hourly distribution of expected vs actual opens
    exp_hour = pairs_open.groupby(pairs_open["bar_hour"].dt.hour).size().rename("expected_count")
    act_hour = trades_open.groupby(trades_open["bar_hour"].dt.hour).size().rename("actual_count")
    hour_df = pd.concat([exp_hour, act_hour], axis=1).fillna(0).astype(int)
    hour_df.to_csv(f"{out_prefix}_hourly_counts.csv")

    # Plot hourly counts
    plt.figure()
    hour_df.plot(kind="bar")
    plt.title("Expected vs Actual Opens by Hour (UTC)")
    plt.xlabel("Hour of day (UTC)")
    plt.ylabel("Count")
    plt.tight_layout()
    plt.savefig(f"{out_prefix}_hourly_counts.png")

    print("Wrote:", out_csv, f"{out_prefix}_hourly_counts.csv", f"{out_prefix}_hourly_counts.png")
    return 0

if __name__ == "__main__":
    pairs_csv = sys.argv[1] if len(sys.argv)>1 else "pairs_bar_log.csv"
    trades_csv = sys.argv[2] if len(sys.argv)>2 else "trades.csv"
    out_prefix = sys.argv[3] if len(sys.argv)>3 else "pairs_vs_actual"
    sys.exit(main(pairs_csv, trades_csv, out_prefix))
