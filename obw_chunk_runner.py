#!/usr/bin/env python3
# obw_chunk_runner.py — швидкий грід по вагам OB + локальні свіпи
import itertools, pandas as pd, numpy as np
import sys, os

# робимо так, щоб 'obw_platform' бачився як пакет
PKG_ROOT = os.path.abspath(".")
if PKG_ROOT not in sys.path:
    sys.path.insert(0, PKG_ROOT)

from obw_platform import backtester_core as CORE
from obw_platform.strategies.ob_weighted import OBWeighted as OBW

def run_one(cache_db, open_hour_kyiv, w, limit_bars, extra=None):
    extra = extra or {}
    wr, ws, wm, wh = w
    strategy_cfg = dict(
        w_rsi=wr, w_stoch=ws, w_mfi=wm, w_hcp=wh,
        min_ob=extra.get("min_ob", 85.0),
        max_atr_ratio=0.03, risk_pct=0.04,
        hold_hours=60, cooldown_days=3,
        top_n=extra.get("top_n", 4),
        min_qv_24h=extra.get("min_qv_24h", 100000),
        min_qv_1h=extra.get("min_qv_1h", 10000),
    )
    portfolio_cfg = dict(
        initial_equity=200, position_notional=20, max_notional_frac=0.5,
        fee_rate=0.001, funding_rate_hour=0.00002,
        slippage_per_side=0.0003, tick_pct=0.0001,
    )
    session_cfg = dict(open_hour_kyiv=open_hour_kyiv, kyiv_offset_hours=3)

    pf = CORE.run_backtest(cache_db, OBW, strategy_cfg, portfolio_cfg, session_cfg, limit_bars=limit_bars)

    if not pf.trades:
        return {
            "equity_end": 200.0,
            "trades": 0,
            "profit_factor": 0.0,
            "max_drawdown_%": 0.0,
            "win_rate_%": 0.0,
            "w_rsi": wr, "w_stoch": ws, "w_mfi": wm, "w_hcp": wh,
            **extra,
        }

    df = pd.DataFrame(pf.trades)
    wins = df.loc[df["realized_pnl"] > 0, "realized_pnl"].sum()
    losses = -df.loc[df["realized_pnl"] < 0, "realized_pnl"].sum()
    pfv = (wins / max(losses, 1e-12)) if losses > 0 else float("inf")
    eq = df["equity_after"].values
    peak = np.maximum.accumulate(eq)
    dd = (eq / np.maximum(peak, 1e-12)) - 1.0
    mdd = float(dd.min()) * 100.0

    return {
        "equity_end": float(eq[-1]),
        "trades": int(len(df)),
        "profit_factor": float(pfv),
        "max_drawdown_%": mdd,
        "win_rate_%": float((df["realized_pnl"] > 0).mean() * 100.0),
        "w_rsi": wr, "w_stoch": ws, "w_mfi": wm, "w_hcp": wh,
        **extra,
    }

def main():
    db60  = "combined_cache_1440.db"        # 60d, 1h
    db120 = "combined_cache_1440_2h.db"     # 120d, 2h
    db240 = "combined_cache_1440_4h.db"     # 240d, 4h

    # A) повний грід ваг на 60d 1h, 500 барів
    weights = list(itertools.product([0.3,0.4], [0.2,0.3], [0.1,0.2], [0.0,0.1]))
    g60 = pd.DataFrame([run_one(db60, 2, w, limit_bars=500) for w in weights]) \
            .sort_values(["equity_end","profit_factor"], ascending=[False,False])
    g60.to_csv("chunk_grid60_500.csv", index=False)

    # B) WF top-3 на 120d/240d
    top3 = g60.head(3)[["w_rsi","w_stoch","w_mfi","w_hcp"]].values.tolist()
    g120 = pd.DataFrame([run_one(db120, 3, tuple(w), limit_bars=500) for w in top3]) \
             .sort_values(["equity_end","profit_factor"], ascending=[False,False])
    g240 = pd.DataFrame([run_one(db240, 3, tuple(w), limit_bars=500) for w in top3]) \
             .sort_values(["equity_end","profit_factor"], ascending=[False,False])
    g120.to_csv("chunk_wf120_500.csv", index=False)
    g240.to_csv("chunk_wf240_500.csv", index=False)

    # C) локальні свіпи навколо кращих ваг
    best_w = tuple(g60.head(1)[["w_rsi","w_stoch","w_mfi","w_hcp"]].values[0])
    pd.DataFrame([run_one(db60, 2, best_w, limit_bars=500, extra={"min_ob":x}) for x in [80,85,90,92,95]]) \
        .sort_values(["equity_end","profit_factor"], ascending=[False,False]) \
        .to_csv("sweep_minob_500.csv", index=False)

    pd.DataFrame([run_one(db60, 2, best_w, limit_bars=500, extra={"min_qv_24h":x}) for x in [100000,200000,500000]]) \
        .sort_values(["equity_end","profit_factor"], ascending=[False,False]) \
        .to_csv("sweep_qv24_500.csv", index=False)

    pd.DataFrame([run_one(db60, 2, best_w, limit_bars=500, extra={"min_qv_1h":x}) for x in [10000,20000,50000]]) \
        .sort_values(["equity_end","profit_factor"], ascending=[False,False]) \
        .to_csv("sweep_qv1h_500.csv", index=False)

    pd.DataFrame([run_one(db60, 2, best_w, limit_bars=500, extra={"top_n":x}) for x in [3,4,5,6]]) \
        .sort_values(["equity_end","profit_factor"], ascending=[False,False]) \
        .to_csv("sweep_topn_500.csv", index=False)

if __name__ == "__main__":
    main()
