def predict_directional_slippage_bp(model, row, side, action, qty):
    return float((model or {}).get('base_bp', 0.0) or 0.0)
