
from __future__ import annotations
import math
from typing import Dict, Any, List
import numpy as np
import pandas as pd

def order_direction(side: str, action: str) -> str:
    side = str(side).upper()
    action = str(action).upper()
    # BUY currency: open/increase long OR close short
    if (side == 'LONG' and action == 'OPEN') or (side == 'SHORT' and action != 'OPEN'):
        return 'BUY'
    return 'SELL'

def adverse_slip_bp_from_fill(price: float, fill: float, side: str, action: str) -> float:
    req = float(price); fill = float(fill)
    if req <= 0 or fill <= 0:
        return 0.0
    side = str(side).upper(); action = str(action).upper()
    if action == 'OPEN':
        return max(0.0, ((fill - req) / req) * 10000.0) if side == 'LONG' else max(0.0, ((req - fill) / req) * 10000.0)
    return max(0.0, ((req - fill) / req) * 10000.0) if side == 'LONG' else max(0.0, ((fill - req) / req) * 10000.0)

def signed_exec_slip_bp_from_fill(price: float, fill: float, side: str, action: str) -> float:
    req = float(price); fill = float(fill)
    if req <= 0 or fill <= 0:
        return 0.0
    direction = order_direction(side, action)
    # BUY worse => positive when fill>req ; SELL worse => positive when fill<req? Actually adverse uses directional.
    return ((fill - req) / req) * 10000.0 if direction == 'BUY' else ((req - fill) / req) * 10000.0

def make_feature_row(row: Dict[str, Any], side: str, action: str, qty: float) -> Dict[str, float]:
    open_px = max(float(row.get('open', row.get('close', 0.0)) or 0.0), 1e-12)
    close_px = float(row.get('close', open_px) or open_px)
    high_px = float(row.get('high', close_px) or close_px)
    low_px = float(row.get('low', close_px) or close_px)
    volume = float(row.get('volume', 0.0) or 0.0)
    quote_volume = float(row.get('quote_volume', close_px * volume) or (close_px * volume))
    notional = max(0.0, float(qty) * close_px)
    participation = notional / max(quote_volume, 1e-12)
    signed_body_bp = 10000.0 * (close_px - open_px) / open_px
    range_bp = 10000.0 * (high_px - low_px) / open_px
    direction = order_direction(side, action)
    dir_sign = 1.0 if direction == 'BUY' else -1.0
    dir_pressure_bp = signed_body_bp * dir_sign
    return {
        'direction_buy': 1.0 if direction == 'BUY' else 0.0,
        'direction_sell': 0.0 if direction == 'BUY' else 1.0,
        'is_open': 1.0 if str(action).upper() == 'OPEN' else 0.0,
        'is_close': 0.0 if str(action).upper() == 'OPEN' else 1.0,
        'log_volume': math.log1p(max(volume, 0.0)),
        'log_quote_volume': math.log1p(max(quote_volume, 0.0)),
        'log_notional': math.log1p(max(notional, 0.0)),
        'participation': participation,
        'log_participation': math.log(max(participation, 1e-12)),
        'signed_body_bp': signed_body_bp,
        'range_bp': range_bp,
        'dir_pressure_bp': dir_pressure_bp,
    }

_FEATURES_LINEAR = ['log_quote_volume','log_notional','log_participation','dir_pressure_bp','range_bp','is_open']
_FEATURES_KNN = ['log_participation','dir_pressure_bp','range_bp','log_notional']

def _ridge_fit(X: np.ndarray, y: np.ndarray, alpha: float = 1.0) -> np.ndarray:
    X1 = np.column_stack([np.ones(len(X)), X])
    eye = np.eye(X1.shape[1]); eye[0,0] = 0.0
    return np.linalg.solve(X1.T @ X1 + alpha * eye, X1.T @ y)

def fit_directional_slippage_model(df: pd.DataFrame, clip_min_bp: float = 0.0, clip_max_bp: float = 60.0) -> Dict[str, Any]:
    work = df.copy()
    if 'direction' not in work.columns:
        work['direction'] = [order_direction(s, a) for s, a in zip(work['side'], work['action'])]
    models = {}
    for direction in ['BUY','SELL']:
        sub = work[work['direction'] == direction].copy()
        if sub.empty:
            models[direction] = {'n':0, 'base_bp':0.0, 'beta':None, 'scaler_mean':{}, 'scaler_std':{}, 'points':[], 'p05':{}, 'p95':{}}
            continue
        # fill features
        cols = _FEATURES_LINEAR + ['adverse_slip_bp']
        sub = sub.dropna(subset=cols)
        if sub.empty:
            models[direction] = {'n':0, 'base_bp':0.0, 'beta':None, 'scaler_mean':{}, 'scaler_std':{}, 'points':[], 'p05':{}, 'p95':{}}
            continue
        X = sub[_FEATURES_LINEAR].astype(float).to_numpy()
        y = sub['adverse_slip_bp'].astype(float).to_numpy()
        beta = _ridge_fit(X, y, alpha=1.0)
        scaler_mean = {f: float(sub[f].mean()) for f in _FEATURES_KNN}
        scaler_std = {f: float(max(sub[f].std(ddof=0), 1e-6)) for f in _FEATURES_KNN}
        points = [
            {f: float(r[f]) for f in _FEATURES_KNN} | {'adverse_slip_bp': float(r['adverse_slip_bp']), 'is_open': float(r['is_open'])}
            for _, r in sub.iterrows()
        ]
        p05 = {f: float(sub[f].quantile(0.05)) for f in _FEATURES_KNN}
        p95 = {f: float(sub[f].quantile(0.95)) for f in _FEATURES_KNN}
        models[direction] = {
            'n': int(len(sub)),
            'beta': [float(x) for x in beta.tolist()],
            'scaler_mean': scaler_mean,
            'scaler_std': scaler_std,
            'points': points,
            'p05': p05,
            'p95': p95,
            'train_mae_bp': float(np.mean(np.abs((np.column_stack([np.ones(len(X)), X]) @ beta) - y))),
            'mean_bp': float(sub['adverse_slip_bp'].mean()),
            'median_bp': float(sub['adverse_slip_bp'].median()),
        }
    return {
        'kind': 'directional_knn_linear',
        'clip_min_bp': float(clip_min_bp),
        'clip_max_bp': float(clip_max_bp),
        'linear_features': list(_FEATURES_LINEAR),
        'knn_features': list(_FEATURES_KNN),
        'models': models,
    }

def update_directional_slippage_model(model: Dict[str, Any], observation: Dict[str, Any]) -> Dict[str, Any]:
    # Append a new point and keep only last N observations per direction to prevent unbounded growth.
    out = dict(model)
    models = out.setdefault('models', {})
    direction = str(observation.get('direction') or order_direction(observation.get('side'), observation.get('action')))
    m = dict(models.get(direction) or {})
    pts = list(m.get('points') or [])
    feat = {f: float(observation[f]) for f in _FEATURES_KNN if f in observation}
    feat['adverse_slip_bp'] = float(observation.get('adverse_slip_bp', 0.0))
    feat['is_open'] = float(observation.get('is_open', 1.0 if str(observation.get('action','OPEN')).upper() == 'OPEN' else 0.0))
    pts.append(feat)
    pts = pts[-5000:]
    m['points'] = pts
    m['n'] = len(pts)
    models[direction] = m
    out['models'] = models
    return out

def predict_directional_slippage_bp(model: Dict[str, Any], row: Dict[str, Any], side: str, action: str, qty: float) -> float:
    kind = str((model or {}).get('kind', 'constant'))
    if kind != 'directional_knn_linear':
        base = float((model or {}).get('base_bp', 0.0))
        return max(0.0, base)
    features = make_feature_row(row, side, action, qty)
    direction = 'BUY' if features['direction_buy'] > 0.5 else 'SELL'
    m = (model.get('models') or {}).get(direction) or {}
    clip_min = float(model.get('clip_min_bp', 0.0))
    clip_max = float(model.get('clip_max_bp', 1000.0))
    if int(m.get('n', 0)) <= 0 or not m.get('beta'):
        return float(np.clip(0.0, clip_min, clip_max))

    # linear extrapolator
    lin = np.array([1.0] + [float(features[f]) for f in model.get('linear_features', _FEATURES_LINEAR)])
    beta = np.array(m['beta'], dtype=float)
    pred_linear = float(lin @ beta)

    # knn interpolator
    points = m.get('points') or []
    if not points:
        return float(np.clip(pred_linear, clip_min, clip_max))
    means = m.get('scaler_mean') or {}
    stds = m.get('scaler_std') or {}
    q05 = m.get('p05') or {}
    q95 = m.get('p95') or {}

    query = np.array([(float(features[f]) - float(means.get(f, 0.0))) / max(float(stds.get(f, 1.0)), 1e-6) for f in model.get('knn_features', _FEATURES_KNN)])
    pts = np.array([[(float(p.get(f, 0.0)) - float(means.get(f, 0.0))) / max(float(stds.get(f, 1.0)), 1e-6) for f in model.get('knn_features', _FEATURES_KNN)] for p in points], dtype=float)
    y = np.array([float(p.get('adverse_slip_bp', 0.0)) for p in points], dtype=float)
    d = np.sqrt(np.sum((pts - query[None, :]) ** 2, axis=1))
    k = min(48, len(d))
    idx = np.argpartition(d, k-1)[:k] if len(d) > k else np.arange(len(d))
    ds = d[idx]
    ys = y[idx]
    # distance weights + extra weight for same open/close class
    open_flag = float(features['is_open'])
    same_class = np.array([1.5 if abs(float(points[i].get('is_open', open_flag)) - open_flag) < 0.5 else 1.0 for i in idx], dtype=float)
    weights = same_class / np.maximum(ds, 0.15) ** 2
    pred_knn = float(np.sum(weights * ys) / np.sum(weights))

    # outside observed participation/pressure => more weight to linear extrapolation
    outside = 0.0
    for f in ('log_participation', 'dir_pressure_bp', 'log_notional'):
        v = float(features.get(f, 0.0))
        lo = float(q05.get(f, v)); hi = float(q95.get(f, v))
        span = max(hi - lo, 1e-6)
        if v < lo:
            outside += (lo - v) / span
        elif v > hi:
            outside += (v - hi) / span
    outside = min(1.0, max(0.0, outside))
    w_linear = 0.25 + 0.65 * outside
    pred = (1.0 - w_linear) * pred_knn + w_linear * pred_linear
    return float(np.clip(pred, clip_min, clip_max))
