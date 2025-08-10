# engine/visualize_results.py
# Простий візуалізатор результатів бек-тесту.
# Будує:
#   1) Equity vs Trade #
#   2) Drawdown vs Trade #
#   3) (за наявності часу у трейдах) Equity vs Time
# Може показувати графіки на екрані та/або зберігати у файл.

from __future__ import annotations
import os
import pandas as pd
import numpy as np

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

def _detect_pnl_series(df: pd.DataFrame) -> pd.Series:
    # Пробуємо готові PnL-колонки
    for c in ["realized_pnl", "net_pnl", "pnl", "pnl_usd", "netpnl"]:
        if c in df.columns:
            return df[c].astype(float).fillna(0.0)
    # Якщо немає — пробуємо відновити з net_return * notional
    if "net_return" in df.columns and "notional" in df.columns:
        return (df["net_return"].astype(float).fillna(0.0) *
                df["notional"].astype(float).fillna(0.0))
    raise ValueError("Не знайдено колонку з PnL (спробував: realized_pnl/net_pnl/pnl/...).")

def _detect_time_series(df: pd.DataFrame) -> pd.Series | None:
    for c in ["exit_time", "t_exit", "close_time", "timestamp", "time"]:
        if c in df.columns:
            ts = pd.to_datetime(df[c], errors="coerce")
            if ts.notna().any():
                return ts
    return None

def plot_equity_curves(
    trades_csv: str = "trades.csv",
    summary_csv: str | None = "summary.csv",
    initial_equity: float | None = None,
    show: bool = True,
    save_dir: str | None = None,
    file_prefix: str = ""
) -> dict:
    """
    Повертає dict з шляхами до збережених картинок (якщо save_dir заданий).
    """
    import matplotlib.pyplot as plt  # імпорт всередині, щоб не тягнути matplotlib там, де не треба

    if not os.path.exists(trades_csv):
        raise FileNotFoundError(f"Не знайдено файл трейдів: {trades_csv}")

    df = pd.read_csv(trades_csv)
    if df.empty:
        raise ValueError("trades.csv порожній — нічого малювати.")

    pnl = _detect_pnl_series(df)
    init_eq = float(initial_equity) if initial_equity is not None else _read_initial_equity(summary_csv, 200.0)

    equity = init_eq + pnl.cumsum()
    dd = (equity / equity.cummax()) - 1.0

    paths = {}
    title_suffix = os.path.basename(file_prefix) if file_prefix else os.path.splitext(os.path.basename(trades_csv))[0]

    # 1) Equity vs Trade #
    plt.figure()
    plt.plot(equity.values, label="Equity")
    plt.title(f"Equity — vs Trade # ({title_suffix})")
    plt.xlabel("Trade #")
    plt.ylabel("Equity")
    plt.legend()
    if save_dir:
        os.makedirs(save_dir, exist_ok=True)
        p = os.path.join(save_dir, f"{file_prefix or 'equity'}_vs_trade.png")
        plt.savefig(p, dpi=120, bbox_inches="tight")
        paths["equity_vs_trade"] = p
    if show:
        plt.show()
    else:
        plt.close()

    # 2) Drawdown vs Trade #
    plt.figure()
    plt.plot(dd.values * 100.0, label="Drawdown %")
    plt.title(f"Drawdown (%) — vs Trade # ({title_suffix})")
    plt.xlabel("Trade #")
    plt.ylabel("Drawdown %")
    plt.legend()
    if save_dir:
        p = os.path.join(save_dir, f"{file_prefix or 'dd'}_vs_trade.png")
        plt.savefig(p, dpi=120, bbox_inches="tight")
        paths["dd_vs_trade"] = p
    if show:
        plt.show()
    else:
        plt.close()

    # 3) Equity vs Time (якщо є час у трейдах)
    ts = _detect_time_series(df)
    if ts is not None:
        mask = ts.notna()
        if mask.any():
            plt.figure()
            plt.plot(ts[mask].values, equity[mask].values)
            plt.title(f"Equity — vs Time ({title_suffix})")
            plt.xlabel("Time")
            plt.ylabel("Equity")
            plt.xticks(rotation=30)
            plt.tight_layout()
            if save_dir:
                p = os.path.join(save_dir, f"{file_prefix or 'equity'}_vs_time.png")
                plt.savefig(p, dpi=120, bbox_inches="tight")
                paths["equity_vs_time"] = p
            if show:
                plt.show()
            else:
                plt.close()

    return paths
