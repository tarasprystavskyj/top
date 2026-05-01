from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

import pandas as pd


def _prepare_matplotlib():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def plot_equity_curve(
    df: pd.DataFrame,
    *,
    x_col: str,
    equity_col: str,
    out_path: str | Path,
    title: str = "Equity curve",
) -> Path:
    plt = _prepare_matplotlib()
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    d = df.copy()
    if x_col in d.columns:
        d[x_col] = pd.to_datetime(d[x_col], utc=True, errors="coerce")

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(d[x_col], d[equity_col], label=equity_col)
    ax.set_title(title)
    ax.set_xlabel("Time")
    ax.set_ylabel(equity_col)
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(out, dpi=160, bbox_inches="tight")
    plt.close(fig)
    return out


def plot_columns(
    df: pd.DataFrame,
    *,
    x_col: str,
    y_cols: list[str],
    out_path: str | Path,
    title: str = "Plot",
    ylabel: str = "Value",
) -> Path:
    plt = _prepare_matplotlib()
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    d = df.copy()
    if x_col in d.columns:
        d[x_col] = pd.to_datetime(d[x_col], utc=True, errors="coerce")

    fig, ax = plt.subplots(figsize=(12, 5))
    for col in y_cols:
        if col in d.columns:
            ax.plot(d[x_col], d[col], label=col)
    ax.set_title(title)
    ax.set_xlabel("Time")
    ax.set_ylabel(ylabel)
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(out, dpi=160, bbox_inches="tight")
    plt.close(fig)
    return out
