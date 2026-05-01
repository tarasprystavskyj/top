from __future__ import annotations

from typing import Any, Dict
import math

import numpy as np


def as_float_array(values) -> np.ndarray:
    return np.asarray(list(values), dtype=float)


def max_drawdown(equity) -> Dict[str, float]:
    """Return max drawdown stats for an equity curve.

    Output:
      mdd_frac: negative fraction, e.g. -0.25
      mdd_pct: negative percent, e.g. -25
      peak: peak equity before max drawdown
      trough: trough equity at max drawdown
    """
    arr = as_float_array(equity)
    if len(arr) == 0:
        return {"mdd_frac": 0.0, "mdd_pct": 0.0, "peak": 0.0, "trough": 0.0}

    peaks = np.maximum.accumulate(arr)
    safe_peaks = np.where(np.abs(peaks) < 1e-12, np.nan, peaks)
    dd = (arr - peaks) / safe_peaks
    dd = np.nan_to_num(dd, nan=0.0, posinf=0.0, neginf=0.0)

    idx = int(np.argmin(dd))
    return {
        "mdd_frac": float(dd[idx]),
        "mdd_pct": float(dd[idx] * 100.0),
        "peak": float(peaks[idx]),
        "trough": float(arr[idx]),
    }


def simple_return(start: float, end: float) -> float:
    if abs(float(start)) < 1e-12:
        return 0.0
    return (float(end) / float(start)) - 1.0


def annualized_return(start: float, end: float, days: float) -> float:
    """Annualized return as fraction.

    Uses geometric annualization. Returns 0 if input is invalid.
    """
    if days <= 0 or start <= 0 or end <= 0:
        return 0.0
    total = end / start
    try:
        return float(total ** (365.0 / float(days)) - 1.0)
    except Exception:
        return 0.0


def equity_curve_stats(equity, *, days: float | None = None) -> Dict[str, Any]:
    arr = as_float_array(equity)
    if len(arr) == 0:
        return {
            "start": 0.0,
            "end": 0.0,
            "pnl": 0.0,
            "return_frac": 0.0,
            "return_pct": 0.0,
            "annualized_return_frac": 0.0,
            "annualized_return_pct": 0.0,
            **max_drawdown([]),
        }

    start = float(arr[0])
    end = float(arr[-1])
    ret = simple_return(start, end)
    ann = annualized_return(start, end, float(days)) if days is not None else 0.0

    return {
        "start": start,
        "end": end,
        "pnl": end - start,
        "return_frac": ret,
        "return_pct": ret * 100.0,
        "annualized_return_frac": ann,
        "annualized_return_pct": ann * 100.0,
        **max_drawdown(arr),
    }


def profit_factor(wins, losses) -> float:
    gross_profit = float(np.sum([x for x in wins if x > 0])) if wins is not None else 0.0
    gross_loss = abs(float(np.sum([x for x in losses if x < 0]))) if losses is not None else 0.0
    if gross_loss <= 1e-12:
        return float("inf") if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    d = float(denominator)
    if abs(d) < 1e-12:
        return float(default)
    return float(numerator) / d
