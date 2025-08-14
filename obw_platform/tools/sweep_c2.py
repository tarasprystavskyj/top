# obw_platform/tools/sweep_c2.py
import os, sys, time, subprocess, argparse, yaml
import pandas as pd
import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PROJ = os.path.join(ROOT, "obw_platform")
CFG_DIR = os.path.join(PROJ, "configs")
REPORTS = os.path.join(PROJ, "reports")
os.makedirs(REPORTS, exist_ok=True)

def run_once(params: dict, bars: int, tag: str, timeout=600):
    cfg = {
        "strategy_class": "strategies.cross_sectional_rs.CrossSectionalRS",
        "cache_db": os.environ.get("OBW_CACHE_DB", "../combined_cache_1440.db"),
        "limit_bars": int(bars),
        "strategy_params": {
            "top_n": params["top_n"],
            "side": "LONG",
            "min_momentum_sum": params["min_momentum_sum"],
            "min_atr_ratio": params["min_atr_ratio"],
            "min_vol_surge_mult": params.get("min_vol_surge_mult", 1.25),
            "min_breadth": params.get("min_breadth", 0.60),
            "min_qv_24h": 200000,
            "min_qv_1h": 10000,
            "sl_atr_mult": params.get("sl_atr_mult", 1.4),
            "tp_atr_mult": params.get("tp_atr_mult", 2.6),
            "max_hold_hours": 96,
            "max_mae_atr_mult": 1.6,
            "mom_flip_thresh": 0.02,
            "trail_start_atr": 1.2,
            "trail_dist_atr": 1.0
        },
        "portfolio": {
            "initial_equity": 200,
            "position_notional": 20,
            "max_notional_frac": 0.5,
            "fee_rate": 0.001,
            "funding_rate_hour": 0.00002,
            "slippage_per_side": 0.0003,
            "tick_pct": 0.0001
        },
        "session": {"open_hour_kyiv": 2, "kyiv_offset_hours": 3},
        "visualize": {"plot_equity": False}
    }
    cfg_path = os.path.join(CFG_DIR, f"cs_sweep_{tag}_{bars}.yaml")
    with open(cfg_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)

    cmd = [sys.executable, "-m", "obw_platform.backtester_core", "--cfg", cfg_path]
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    dt = time.time() - t0

    # stash outputs (корінь або пакет — перевіримо обидва)
    cand = [
        os.path.join(ROOT, "trades.csv"),
        os.path.join(PROJ, "trades.csv"),
    ]
    t_src = next((p for p in cand if os.path.exists(p)), None)
    cand = [
        os.path.join(ROOT, "summary.csv"),
        os.path.join(PROJ, "summary.csv"),
    ]
    s_src = next((p for p in cand if os.path.exists(p)), None)

    t_out = os.path.join(REPORTS, f"trades_{tag}_{bars}.csv") if t_src else None
    s_out = os.path.join(REPORTS, f"summary_{tag}_{bars}.csv") if s_src else None
    if t_src: os.replace(t_src, t_out)
    if s_src: os.replace(s_src, s_out)

    return {
        "summary": s_out, "trades": t_out, "rc": proc.returncode,
        "runtime_s": round(dt,1),
        "stderr_tail": "\n".join(proc.stderr.splitlines()[-8:]),
        "stdout_tail": "\n".join(proc.stdout.splitlines()[-8:])
    }

def metrics(summary_path, trades_path, bars=1440):
    out = {"PF": np.nan, "WinRate%": np.nan, "MaxDD%": np.nan, "TotalRet%": np.nan, "Trades/day": np.nan, "Monthly%": np.nan}
    if not trades_path or not os.path.exists(trades_path): return out
    init = 200.0
    if summary_path and os.path.exists(summary_path):
        try:
            sdf = pd.read_csv(summary_path)
            for c in sdf.columns:
                if "initial" in c.lower() and "equity" in c.lower():
                    init = float(sdf[c].iloc[0]); break
        except: pass
    df = pd.read_csv(trades_path)
    if df.empty: return out
    pnl = None
    for c in ["realized_pnl","net_pnl","pnl","pnl_usd","netpnl"]:
        if c in df.columns:
            pnl = df[c].astype(float).fillna(0.0); break
    if pnl is None and "net_return" in df.columns and "notional" in df.columns:
        pnl = df["net_return"].astype(float).fillna(0.0) * df["notional"].astype(float).fillna(0.0)
    if pnl is None: return out
    eq = init + pnl.cumsum()
    dd = (eq/eq.cummax()) - 1.0
    PF = float(pnl[pnl>0].sum() / max(-pnl[pnl<0].sum(), 1e-9))
    Win = float((pnl>0).mean() * 100.0)
    days = bars/24.0
    TPD = float(len(pnl)/days) if days>0 else np.nan
    TR = float((eq.iloc[-1]/init - 1.0) * 100.0)
    Monthly = ((1.0 + TR/100.0)**(30.0/days) - 1.0) * 100.0 if days>0 else np.nan
    out.update({"PF":round(PF,3),"WinRate%":round(Win,2),"MaxDD%":round(dd.min()*100.0,2),
                "TotalRet%":round(TR,2),"Trades/day":round(TPD,3),"Monthly%":round(Monthly,2)})
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=int, default=1440, help="500 / 1440 / 2880")
    ap.add_argument("--preset", choices=["base","aggr","full"], default="full",
                    help="base=стабільний, aggr=агресивний, full=обидва + околиці")
    args = ap.parse_args()

    grids = []
    if args.preset in ("base","full"):
        grids += [{"top_n":4,"min_momentum_sum":0.12,"min_atr_ratio":0.022,"min_vol_surge_mult":1.25,"min_breadth":0.60,"sl_atr_mult":1.4,"tp_atr_mult":2.6}]
    if args.preset in ("aggr","full"):
        grids += [{"top_n":4,"min_momentum_sum":0.13,"min_atr_ratio":0.024,"min_vol_surge_mult":1.20,"min_breadth":0.58,"sl_atr_mult":1.2,"tp_atr_mult":3.0}]

    if args.preset == "full":
        # невеличка околиця довкола обох
        around = [
            {"top_n":3,"min_momentum_sum":0.12,"min_atr_ratio":0.022,"sl_atr_mult":1.4,"tp_atr_mult":2.6},
            {"top_n":5,"min_momentum_sum":0.12,"min_atr_ratio":0.022,"sl_atr_mult":1.4,"tp_atr_mult":2.6},
            {"top_n":4,"min_momentum_sum":0.12,"min_atr_ratio":0.024,"sl_atr_mult":1.4,"tp_atr_mult":2.8},
            {"top_n":4,"min_momentum_sum":0.13,"min_atr_ratio":0.024,"sl_atr_mult":1.3,"tp_atr_mult":2.8},
            {"top_n":4,"min_momentum_sum":0.14,"min_atr_ratio":0.024,"sl_atr_mult":1.2,"tp_atr_mult":3.0},
        ]
        for r in around:
            r.setdefault("min_vol_surge_mult", 1.25 if r["min_momentum_sum"]<=0.12 else 1.20)
            r.setdefault("min_breadth", 0.60 if r["min_momentum_sum"]<=0.12 else 0.58)
        grids += around

    rows = []
    for i, p in enumerate(grids, 1):
        tag = f"t{p['top_n']}_m{p['min_momentum_sum']:.2f}_a{p['min_atr_ratio']:.3f}_sl{p.get('sl_atr_mult',1.4)}_tp{p.get('tp_atr_mult',2.6)}"
        res = run_once(p, args.bars, tag)
        m = metrics(res["summary"], res["trades"], args.bars)
        rows.append({"Tag":tag, "Bars":args.bars, **p, **m, "RC":res["rc"], "Runtime_s":res["runtime_s"]})

    df = pd.DataFrame(rows).sort_values(["Monthly%","PF"], ascending=[False,False]).reset_index(drop=True)
    out_csv = os.path.join(REPORTS, f"C2_sweep_{args.bars}.csv")
    df.to_csv(out_csv, index=False)
    print(df.head(12).to_string(index=False))
    print("Saved:", out_csv)
    # покажемо найкращий конфіг для копіпасту
    if not df.empty:
        best = df.iloc[0].to_dict()
        print("\nBest config:", {k:best[k] for k in ['top_n','min_momentum_sum','min_atr_ratio','min_vol_surge_mult','min_breadth','sl_atr_mult','tp_atr_mult']})
        print("Metrics: PF={PF}, MaxDD={MaxDD%}%, Monthly={Monthly%}%".format(**best))

if __name__ == "__main__":
    sys.exit(main() or 0)
