"""Extended visualization helpers shared between backtests and live sessions.

This module mirrors the plotting routines used by the backtester so the live
result pipeline can render identical equity, drawdown and time-series charts.
"""

from __future__ import annotations

import os
from typing import Mapping, Optional

import pandas as pd

__all__ = [
    "plot_equity_curves",
    "plot_equity_from_dataframe",
]


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
    for c in ["realized_pnl", "realised_pnl", "net_pnl", "pnl", "pnl_usd", "netpnl"]:
        if c in df.columns:
            return df[c].astype(float).fillna(0.0)
    if "net_return" in df.columns and "notional" in df.columns:
        return (
            df["net_return"].astype(float).fillna(0.0)
            * df["notional"].astype(float).fillna(0.0)
        )
    raise ValueError(
        "Не знайдено колонку з PnL (шукав realized_pnl/realised_pnl/net_pnl/...)."
    )


def _detect_time_series(
    df: pd.DataFrame, explicit: str | None = None
) -> Optional[pd.Series]:
    if explicit and explicit in df.columns:
        ts = pd.to_datetime(df[explicit], errors="coerce")
        if ts.notna().any():
            return ts
    for c in ["exit_time", "exit_fill_ts", "t_exit", "close_time", "timestamp", "time"]:
        if c in df.columns:
            ts = pd.to_datetime(df[c], errors="coerce")
            if ts.notna().any():
                return ts
    return None


def _render_equity_figures(
    equity: pd.Series,
    timestamps: Optional[pd.Series],
    *,
    show: bool,
    save_dir: str | None,
    file_prefix: str,
    title_suffix: str,
) -> Mapping[str, str]:
    import matplotlib.pyplot as plt

    eq = equity.astype(float).fillna(method="ffill").fillna(method="bfill")
    if eq.empty:
        raise ValueError("Equity series is empty")
    dd = (eq / eq.cummax()) - 1.0

    paths: dict[str, str] = {}
    if save_dir:
        os.makedirs(save_dir, exist_ok=True)

    def save_current(name: str) -> Optional[str]:
        if not save_dir:
            return None
        path = os.path.join(save_dir, f"{file_prefix}_{name}.png" if file_prefix else f"{name}.png")
        plt.savefig(path, dpi=120, bbox_inches="tight")
        paths[name] = path
        return path

    # Equity vs trade
    plt.figure()
    plt.plot(eq.values, label="Equity")
    plt.title(f"Equity — vs Trade # ({title_suffix})")
    plt.xlabel("Trade #")
    plt.ylabel("Equity")
    plt.legend()
    save_current("equity_vs_trade")
    if show:
        plt.show()
    else:
        plt.close()

    # Drawdown vs trade
    plt.figure()
    plt.plot(dd.values * 100.0, label="Drawdown %")
    plt.title(f"Drawdown (%) — vs Trade # ({title_suffix})")
    plt.xlabel("Trade #")
    plt.ylabel("Drawdown %")
    plt.legend()
    save_current("dd_vs_trade")
    if show:
        plt.show()
    else:
        plt.close()

    if timestamps is not None:
        ts = pd.to_datetime(timestamps, errors="coerce")
        mask = ts.notna()
        if mask.any():
            plt.figure()
            plt.plot(ts[mask].values, eq[mask].values)
            plt.title(f"Equity — vs Time ({title_suffix})")
            plt.xlabel("Time")
            plt.ylabel("Equity")
            plt.xticks(rotation=30)
            plt.tight_layout()
            save_current("equity_vs_time")
            if show:
                plt.show()
            else:
                plt.close()

    return paths


def plot_equity_curves(
    trades_csv: str = "trades.csv",
    summary_csv: str | None = "summary.csv",
    initial_equity: float | None = None,
    show: bool = True,
    save_dir: str | None = None,
    file_prefix: str = "",
) -> Mapping[str, str]:
    """Backtester-compatible entry point that reads a CSV of trades."""

    if not os.path.exists(trades_csv):
        raise FileNotFoundError(f"Не знайдено файл трейдів: {trades_csv}")

    df = pd.read_csv(trades_csv)
    if df.empty:
        raise ValueError("trades.csv порожній — нічого малювати.")

    pnl = _detect_pnl_series(df)
    init = (
        float(initial_equity)
        if initial_equity is not None
        else _read_initial_equity(summary_csv, 200.0)
    )
    equity = init + pnl.cumsum()
    ts = _detect_time_series(df)
    title_suffix = (
        os.path.basename(file_prefix)
        if file_prefix
        else os.path.splitext(os.path.basename(trades_csv))[0]
    )
    return _render_equity_figures(
        equity,
        ts,
        show=show,
        save_dir=save_dir,
        file_prefix=file_prefix,
        title_suffix=title_suffix,
    )


def plot_equity_from_dataframe(
    df: pd.DataFrame,
    *,
    initial_equity: float | None = None,
    time_column: str | None = None,
    show: bool = True,
    save_dir: str | None = None,
    file_prefix: str = "",
    title_suffix: str | None = None,
) -> Mapping[str, str]:
    """Render equity plots directly from an in-memory trades DataFrame."""

    if df is None or df.empty:
        raise ValueError("DataFrame з трейдами порожній — нічого малювати.")

    pnl = _detect_pnl_series(df)
    init = float(initial_equity) if initial_equity is not None else 0.0
    equity = init + pnl.cumsum()
    ts = _detect_time_series(df, explicit=time_column)
    suffix = title_suffix or "live"
    return _render_equity_figures(
        equity,
        ts,
        show=show,
        save_dir=save_dir,
        file_prefix=file_prefix,
        title_suffix=suffix,
    )
