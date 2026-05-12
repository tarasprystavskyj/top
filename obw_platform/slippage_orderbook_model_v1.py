#!/usr/bin/env python3
"""Orderbook-calibrated slippage predictor.

Model contract
--------------
The model is trained from live slippage observations where a pre-trade
orderbook sweep estimate is available. It predicts adverse one-side slippage
in basis points for the backtester.

Important behavior:
* If a row contains a precomputed sweep estimate, use calibrated BUY/SELL or
  side-action coefficients.
* If a row has no orderbook fields, return the calibrated static fallback.
  This is the correct mode for old annual fast caches without orderbook data.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, Optional


def order_direction(strategy_side: str, order_action: str) -> str:
    side = str(strategy_side or "").upper()
    action = str(order_action or "").upper()
    if (side == "LONG" and action == "OPEN") or (side == "SHORT" and action in {"CLOSE", "PARTIAL"}):
        return "BUY"
    return "SELL"


def group_key(strategy_side: str, order_action: str) -> str:
    return f"{str(strategy_side or '').upper()}_{str(order_action or '').upper()}"


def _num_or_none(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        x = float(value)
    except Exception:
        return None
    if not math.isfinite(x):
        return None
    return x


def _first_num(row: Dict[str, Any], *names: str) -> Optional[float]:
    for name in names:
        if name in row:
            x = _num_or_none(row.get(name))
            if x is not None:
                return x
    return None


def load_model(model_or_path: Any) -> Dict[str, Any]:
    if isinstance(model_or_path, dict):
        return model_or_path
    path = Path(str(model_or_path))
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def static_slippage_bp_from_model(model: Dict[str, Any], mode: str = "base") -> float:
    mode = str(mode or "base").lower()
    if mode in {"stress", "p90"}:
        return float((model.get("recommended") or {}).get("fast_backtest_stress_static_bp", model.get("stress_static_bp", model.get("fallback_bp", 0.0))) or 0.0)
    if mode in {"conservative", "p95"}:
        return float((model.get("recommended") or {}).get("fast_backtest_p95_static_bp", model.get("p95_static_bp", model.get("fallback_bp", 0.0))) or 0.0)
    return float((model.get("recommended") or {}).get("fast_backtest_base_static_bp", model.get("fallback_bp", model.get("static_base_bp", 0.0))) or 0.0)


def predict_orderbook_slippage_bp(
    model: Dict[str, Any],
    row: Dict[str, Any],
    side: str,
    action: str,
    qty: float = 0.0,
    *,
    stress: bool = False,
) -> float:
    """Predict adverse slippage in basis points.

    The function is intentionally conservative about missing data: no sweep
    estimate means old OHLCV-only backtest data, so it returns the static base
    calibration instead of pretending that sweep=0.
    """
    model = model or {}
    if str(model.get("kind", "")) != "orderbook_calibrated_v1":
        return max(0.0, float(model.get("base_bp", model.get("fallback_bp", 0.0)) or 0.0))

    sweep_raw = _first_num(
        row or {},
        "snapshot_est_sweep_bp",
        "est_sweep_slip_bp",
        "orderbook_sweep_bp",
        "sweep_bp",
    )
    if sweep_raw is None:
        return max(0.0, static_slippage_bp_from_model(model, "stress" if stress else "base"))

    sweep_bp = max(0.0, float(sweep_raw))
    direction = order_direction(side, action)
    gkey = group_key(side, action)

    coeff = None
    groups = model.get("groups") or {}
    # Group coefficients are used only when they were trained on enough data.
    min_group_n = int(model.get("min_group_n_for_prediction", 30) or 30)
    g = groups.get(gkey) or {}
    if int(g.get("n", 0) or 0) >= min_group_n:
        coeff = g

    if coeff is None:
        coeff = (model.get("by_direction") or {}).get(direction) or {}
    if not coeff:
        coeff = model.get("global") or {}

    intercept = float(coeff.get("intercept", 0.0) or 0.0)
    slope = float(coeff.get("slope", 1.0) or 1.0)
    pred = intercept + slope * sweep_bp

    if stress:
        # Optional residual uplift for stress tests. The calibrator fills this
        # with p90 absolute residual for the selected model bucket.
        pred += float(coeff.get("p90_abs_residual_bp", model.get("global_p90_abs_residual_bp", 0.0)) or 0.0)

    clip_min = float(model.get("clip_min_bp", 0.0) or 0.0)
    clip_max = float(model.get("clip_max_bp", 1000.0) or 1000.0)
    return float(min(max(pred, clip_min), clip_max))
