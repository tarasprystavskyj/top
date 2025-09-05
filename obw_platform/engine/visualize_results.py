
from __future__ import annotations
import os
import pandas as pd
from datetime import datetime

def _read_initial_equity(summary_csv: str | None, default: float = 200.0) -> float:
    if not summary_csv or not os.path.exists(summary_csv):
        return default
    try:
        sdf = pd.read_csv(summary_csv)
        for c in sdf.columns:
            cl = c.lower()
            if "initial" in cl and "equity" in cl:
                return float(sdf[c].iloc[0])
    except Exception:
        pass
    return default

def _detect_pnl_series(df: pd.DataFrame):
    for c in ["realized_pnl","net_pnl","pnl","pnl_usd","netpnl"]:
        if c in df.columns:
            return df[c].astype(float).fillna(0.0)
    if "net_return" in df.columns and "notional" in df.columns:
        return (df["net_return"].astype(float).fillna(0.0) * df["notional"].astype(float).fillna(0.0))
    raise ValueError("Не знайдено PnL-колонку.")

def _detect_time_series(df: pd.DataFrame):
    for c in ["exit_time","t_exit","close_time","timestamp","time"]:
        if c in df.columns:
            ts = pd.to_datetime(df[c], errors="coerce")
            if ts.notna().any(): return ts
    return None

def plot_equity_curves(trades_csv="trades.csv", summary_csv="summary.csv",
                       initial_equity=None, show=True, save_dir=None, file_prefix=""):
    import matplotlib.pyplot as plt
    if not os.path.exists(trades_csv):
        raise FileNotFoundError(f"Не знайдено файл трейдів: {trades_csv}")
    df = pd.read_csv(trades_csv)
    if df.empty:
        raise ValueError("trades.csv порожній — нічого малювати.")

    pnl = _detect_pnl_series(df)
    init_eq = float(initial_equity) if initial_equity is not None else _read_initial_equity(summary_csv, 200.0)
    equity = init_eq + pnl.cumsum()
    dd = (equity/equity.cummax()) - 1.0

    paths = {}
    title_suffix = os.path.splitext(os.path.basename(trades_csv))[0]
    prefix = (file_prefix or title_suffix)
    run_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = f"{prefix}_{run_tag}"

    # equity vs trade
    plt.figure(); plt.plot(equity.values, label="Equity"); plt.title(f"Equity — vs Trade # ({title_suffix})")
    plt.xlabel("Trade #"); plt.ylabel("Equity"); plt.legend()
    if save_dir:
        os.makedirs(save_dir, exist_ok=True)
        p = os.path.join(save_dir, f"{prefix}_equity_vs_trade.png"); plt.savefig(p, dpi=120, bbox_inches="tight"); paths["equity_vs_trade"]=p
    if show: plt.show()
    else: plt.close()

    # dd vs trade
    plt.figure(); plt.plot(dd.values*100.0, label="Drawdown %"); plt.title(f"Drawdown (%) — vs Trade # ({title_suffix})")
    plt.xlabel("Trade #"); plt.ylabel("Drawdown %"); plt.legend()
    if save_dir:
        p = os.path.join(save_dir, f"{prefix}_dd_vs_trade.png"); plt.savefig(p, dpi=120, bbox_inches="tight"); paths["dd_vs_trade"]=p
    if show: plt.show()
    else: plt.close()

    ts = _detect_time_series(df)
    if ts is not None and ts.notna().any():
        plt.figure(); plt.plot(ts.values, equity.values)
        plt.title(f"Equity — vs Time ({title_suffix})"); plt.xlabel("Time"); plt.ylabel("Equity"); plt.xticks(rotation=30); plt.tight_layout()
        if save_dir:
            p = os.path.join(save_dir, f"{prefix}_equity_vs_time.png"); plt.savefig(p, dpi=120, bbox_inches="tight"); paths["equity_vs_time"]=p
        if show: plt.show()
        else: plt.close()

    return paths
