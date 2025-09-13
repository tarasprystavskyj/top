#!/usr/bin/env python3
import argparse, pandas as pd, numpy as np, sys

def main():
    ap = argparse.ArgumentParser(description="Build allow-list (universe) from trades.csv")
    ap.add_argument("--trades", default="trades.csv", help="Path to trades.csv")
    ap.add_argument("--out", default="universe_prof_v2.txt", help="Output universe file")
    ap.add_argument("--min-trades", type=int, default=8)
    ap.add_argument("--min-pf", type=float, default=1.05)
    ap.add_argument("--top-k", type=int, default=80)
    ap.add_argument("--metric", choices=["pf","sum","winrate"], default="pf",
                    help="Sort metric to pick Top-K: pf | sum | winrate")
    args = ap.parse_args()

    try:
        df = pd.read_csv(args.trades)
    except Exception as e:
        print(f"[ERR] cannot read {args.trades}: {e}")
        sys.exit(1)
    if "symbol" not in df.columns:
        print("[ERR] trades.csv must contain 'symbol' column")
        sys.exit(1)

    # numeric returns / pnl
    for col in ("realized_pnl","net_return","gross_return"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "realized_pnl" not in df.columns:
        # fallback to notional * net_return if present
        if "net_return" in df.columns and "notional" in df.columns:
            df["realized_pnl"] = pd.to_numeric(df["net_return"], errors="coerce") * pd.to_numeric(df.get("notional", 1.0), errors="coerce")
        else:
            df["realized_pnl"] = 0.0

    g = df.groupby("symbol", dropna=False)
    stats = g["realized_pnl"].agg(["count","sum"]).rename(columns={"count":"trades","sum":"sum_pnl"})
    pos = g["realized_pnl"].apply(lambda s: s[s>0].sum())
    neg = g["realized_pnl"].apply(lambda s: s[s<0].sum())
    win = g["realized_pnl"].apply(lambda s: (s>0).sum())

    stats["pos"] = pos.fillna(0.0)
    stats["neg"] = neg.fillna(0.0)
    stats["pf"]  = np.where(stats["neg"]<0, stats["pos"]/(-stats["neg"]), 0.0)
    stats["winrate"] = np.where(stats["trades"]>0, win.values*100.0/stats["trades"], 0.0)

    # filters
    filt = stats["trades"] >= args.min_trades
    filt &= (stats["pf"] >= args.min_pf) | (stats["sum_pnl"] > 0.0)
    cand = stats[filt].copy()
    if cand.empty:
        print("[WARN] no symbols passed the filter; writing empty universe.")
        open(args.out,"w").close(); sys.exit(0)

    key = {"pf":"pf", "sum":"sum_pnl", "winrate":"winrate"}[args.metric]
    cand = cand.sort_values([key, "sum_pnl", "trades"], ascending=[False, False, False]).head(args.top_k)

    with open(args.out, "w", encoding="utf-8") as f:
        for s in cand.index.tolist():
            f.write(f"{s}\n")

    print(f"[universe] wrote {len(cand)} symbols -> {args.out}")
    print(cand[[key,"sum_pnl","trades"]].head(20))

if __name__ == "__main__":
    main()
