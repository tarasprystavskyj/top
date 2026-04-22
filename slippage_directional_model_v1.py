from __future__ import annotations
import math

def predict_directional_slippage_bp(model: dict, row: dict, side: str, action: str, qty: float) -> float:
    """Fallback directional slippage model.
    Supports a lightweight feature-linear form so backtester_dual_core_dynamic_v5.py
    remains importable even when the original directional KNN artifact is absent.
    """
    close_px = max(float(row.get('close', 0.0) or 0.0), 1e-12)
    open_px = float(row.get('open', close_px) or close_px)
    high_px = float(row.get('high', close_px) or close_px)
    low_px = float(row.get('low', close_px) or close_px)
    volume = float(row.get('volume', 0.0) or 0.0)
    quote_volume = float(row.get('quote_volume', close_px * volume) or (close_px * volume))
    notional = float(qty or 0.0) * close_px
    participation = notional / max(quote_volume, 1e-12)
    signed_body_bp = 10000.0 * (close_px - open_px) / max(open_px, 1e-12)
    range_bp = 10000.0 * (high_px - low_px) / max(open_px, 1e-12)
    side_sign = 1.0 if str(side).upper() == 'LONG' else -1.0
    feats = {
        'participation': participation,
        'log_quote_volume': math.log1p(max(quote_volume, 0.0)),
        'signed_body_bp': signed_body_bp,
        'range_bp': range_bp,
        'is_exit': 0.0 if str(action).upper() == 'OPEN' else 1.0,
        'side_x_body_signed_bp': side_sign * signed_body_bp,
        'side_x_range_bp': side_sign * range_bp,
    }
    base_bp = float(model.get('base_bp', 0.0) or 0.0)
    coeffs = dict(model.get('coefficients') or {})
    val = base_bp
    for k, w in coeffs.items():
        val += float(w) * float(feats.get(k, 0.0))
    clip_min_bp = float(model.get('clip_min_bp', 0.0) or 0.0)
    clip_max_bp = float(model.get('clip_max_bp', 1000.0) or 1000.0)
    if clip_max_bp < clip_min_bp:
        clip_max_bp = clip_min_bp
    return max(clip_min_bp, min(clip_max_bp, float(val)))
