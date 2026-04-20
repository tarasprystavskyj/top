
from __future__ import annotations
import json
import math
import sqlite3
from typing import Dict, Any, List, Optional
import numpy as np
import pandas as pd

def order_direction(side: str, action: str) -> str:
    side = str(side).upper()
    action = str(action).upper()
    if (side == 'LONG' and action == 'OPEN') or (side == 'SHORT' and action != 'OPEN'):
        return 'BUY'
    return 'SELL'

def adverse_slip_bp_from_fill(price: float, fill: float, side: str, action: str) -> float:
    req = float(price or 0.0); fill = float(fill or 0.0)
    if req <= 0 or fill <= 0:
        return 0.0
    side = str(side).upper(); action = str(action).upper()
    if action == 'OPEN':
        return max(0.0, ((fill - req) / req) * 10000.0) if side == 'LONG' else max(0.0, ((req - fill) / req) * 10000.0)
    return max(0.0, ((req - fill) / req) * 10000.0) if side == 'LONG' else max(0.0, ((fill - req) / req) * 10000.0)

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
    spread_bp = float(row.get('spread_bp') or 0.0 or 0.0)
    est_sweep_bp = float(row.get('est_sweep_slip_bp') or 0.0 or 0.0)
    bid_depth_qty = max(float(row.get('bid_depth_qty') or 0.0), 0.0)
    ask_depth_qty = max(float(row.get('ask_depth_qty') or 0.0), 0.0)
    imbalance = float(row.get('book_imbalance') or 0.0 or 0.0)
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
        'spread_bp': spread_bp,
        'est_sweep_slip_bp': est_sweep_bp,
        'log_bid_depth': math.log1p(bid_depth_qty),
        'log_ask_depth': math.log1p(ask_depth_qty),
        'book_imbalance': imbalance,
    }

_LINEAR_FEATURES = ['log_quote_volume','log_notional','log_participation','dir_pressure_bp','range_bp','is_open','spread_bp','est_sweep_slip_bp','log_bid_depth','log_ask_depth','book_imbalance']
_KNN_FEATURES = ['log_participation','dir_pressure_bp','range_bp','log_notional','spread_bp','est_sweep_slip_bp','book_imbalance']

def _ridge_fit(X: np.ndarray, y: np.ndarray, alpha: float = 1.0) -> np.ndarray:
    X1 = np.column_stack([np.ones(len(X)), X])
    eye = np.eye(X1.shape[1]); eye[0,0] = 0.0
    return np.linalg.solve(X1.T @ X1 + alpha * eye, X1.T @ y)

def build_training_frame(session_db_path: str, cache_db_path: Optional[str] = None, symbol: Optional[str] = None) -> pd.DataFrame:
    con = sqlite3.connect(session_db_path)
    # try new observation tables first
    try:
        obs = pd.read_sql_query(
            """
            select so.*, mm.spread_bp, mm.est_sweep_slip_bp, mm.bid_depth_qty, mm.ask_depth_qty, mm.book_imbalance
            from slippage_observations so
            left join market_microstructure mm
              on mm.run_id = so.run_id
             and mm.bar_time_utc = so.bar_time_utc
             and mm.symbol = so.symbol
             and upper(mm.strategy_side) = upper(so.strategy_side)
             and upper(mm.order_action) = upper(so.order_action)
             and abs(coalesce(mm.qty,0) - coalesce(so.qty,0)) < 1e-9
             and mm.phase = 'pre'
            """,
            con,
        )
    except Exception:
        obs = pd.DataFrame()
    if obs.empty:
        orders = pd.read_sql_query(
            """
            select ts_utc, bar_time_utc, symbol, side, type as action, price, qty, extra
            from orders
            where status='FILLED'
            """,
            con,
        )
        con.close()
        if orders.empty:
            return orders
        orders["extra_obj"] = orders["extra"].apply(lambda s: json.loads(s) if isinstance(s, str) and s else {})
        orders["fill"] = orders["extra_obj"].apply(lambda d: float(d.get("fill")) if isinstance(d, dict) and d.get("fill") is not None else np.nan).fillna(orders["price"])
        orders["requested_price"] = orders["price"]
        orders["fill_price"] = orders["fill"]
        orders["strategy_side"] = orders["side"]
        orders["order_action"] = orders["action"]
        orders["actual_adverse_bp"] = [adverse_slip_bp_from_fill(p, f, s, a) for p,f,s,a in zip(orders["price"], orders["fill"], orders["side"], orders["action"])]
        obs = orders
    else:
        con.close()
        obs["requested_price"] = obs["requested_price"].fillna(obs.get("price"))
        obs["fill_price"] = obs["fill_price"].fillna(obs.get("fill"))
        obs["strategy_side"] = obs["strategy_side"].fillna(obs.get("side"))
        obs["order_action"] = obs["order_action"].fillna(obs.get("action"))
        obs["actual_adverse_bp"] = obs["actual_adverse_bp"].fillna(
            [adverse_slip_bp_from_fill(p, f, s, a) for p,f,s,a in zip(obs["requested_price"], obs["fill_price"], obs["strategy_side"], obs["order_action"])]
        )
    if symbol is not None:
        obs = obs[obs["symbol"] == symbol].copy()
    obs["bar_ts"] = pd.to_datetime(obs["bar_time_utc"], utc=True, errors='coerce')
    if cache_db_path:
        c2 = sqlite3.connect(cache_db_path)
        bars = pd.read_sql_query(
            "select symbol, datetime_utc, open, high, low, close, volume, quote_volume from price_indicators",
            c2,
        )
        c2.close()
        bars["bar_ts"] = pd.to_datetime(bars["datetime_utc"], utc=True)
        obs = obs.merge(bars[["symbol","bar_ts","open","high","low","close","volume","quote_volume"]], on=["symbol","bar_ts"], how="left")
    feat_rows = [make_feature_row(r.to_dict(), r.get("strategy_side") or r.get("side"), r.get("order_action") or r.get("action"), float(r.get("qty") or 0.0)) for _, r in obs.iterrows()]
    out = pd.concat([obs.reset_index(drop=True), pd.DataFrame(feat_rows)], axis=1)
    out["direction"] = [order_direction(s, a) for s, a in zip(out["strategy_side"], out["order_action"])]
    return out

def fit_directional_slippage_model(df: pd.DataFrame, clip_min_bp: float = 0.0, clip_max_bp: float = 80.0) -> Dict[str, Any]:
    work = df.copy()
    if "direction" not in work.columns:
        work["direction"] = [order_direction(s, a) for s, a in zip(work["strategy_side"], work["order_action"])]
    models = {}
    for direction in ["BUY","SELL"]:
        sub = work[work["direction"] == direction].copy()
        sub = sub.dropna(subset=_LINEAR_FEATURES + ["actual_adverse_bp"])
        if sub.empty:
            models[direction] = {"n":0, "beta":None, "points":[], "scaler_mean":{}, "scaler_std":{}, "p05":{}, "p95":{}, "train_mae_bp": None}
            continue
        X = sub[_LINEAR_FEATURES].astype(float).to_numpy()
        y = sub["actual_adverse_bp"].astype(float).to_numpy()
        beta = _ridge_fit(X, y, alpha=2.0)
        scaler_mean = {f: float(sub[f].mean()) for f in _KNN_FEATURES}
        scaler_std = {f: float(max(sub[f].std(ddof=0), 1e-6)) for f in _KNN_FEATURES}
        points = [{f: float(r[f]) for f in _KNN_FEATURES} | {'actual_adverse_bp': float(r['actual_adverse_bp']), 'is_open': float(r['is_open'])} for _, r in sub.iterrows()]
        p05 = {f: float(sub[f].quantile(0.05)) for f in _KNN_FEATURES}
        p95 = {f: float(sub[f].quantile(0.95)) for f in _KNN_FEATURES}
        pred = np.column_stack([np.ones(len(X)), X]) @ beta
        models[direction] = {
            "n": int(len(sub)),
            "beta": [float(x) for x in beta.tolist()],
            "points": points[-8000:],
            "scaler_mean": scaler_mean,
            "scaler_std": scaler_std,
            "p05": p05,
            "p95": p95,
            "train_mae_bp": float(np.mean(np.abs(pred - y))),
            "real_mean_bp": float(sub["actual_adverse_bp"].mean()),
            "real_median_bp": float(sub["actual_adverse_bp"].median()),
        }
    return {
        "kind": "directional_knn_linear_v2",
        "clip_min_bp": float(clip_min_bp),
        "clip_max_bp": float(clip_max_bp),
        "linear_features": list(_LINEAR_FEATURES),
        "knn_features": list(_KNN_FEATURES),
        "models": models,
    }

def _rebuild_direction_model(points: List[Dict[str, Any]], direction: str):
    if not points:
        return {"n":0, "beta":None, "points":[], "scaler_mean":{}, "scaler_std":{}, "p05":{}, "p95":{}, "train_mae_bp": None}
    sub = pd.DataFrame(points)
    # fabricate linear-only dataset from points
    for f in _LINEAR_FEATURES:
        if f not in sub.columns:
            sub[f] = 0.0
    X = sub[_LINEAR_FEATURES].astype(float).to_numpy()
    y = sub["actual_adverse_bp"].astype(float).to_numpy()
    beta = _ridge_fit(X, y, alpha=2.0)
    scaler_mean = {f: float(sub[f].mean()) for f in _KNN_FEATURES}
    scaler_std = {f: float(max(sub[f].std(ddof=0), 1e-6)) for f in _KNN_FEATURES}
    p05 = {f: float(sub[f].quantile(0.05)) for f in _KNN_FEATURES}
    p95 = {f: float(sub[f].quantile(0.95)) for f in _KNN_FEATURES}
    pred = np.column_stack([np.ones(len(X)), X]) @ beta
    return {
        "n": int(len(sub)),
        "beta": [float(x) for x in beta.tolist()],
        "points": sub.to_dict("records")[-8000:],
        "scaler_mean": scaler_mean,
        "scaler_std": scaler_std,
        "p05": p05,
        "p95": p95,
        "train_mae_bp": float(np.mean(np.abs(pred - y))),
        "real_mean_bp": float(sub["actual_adverse_bp"].mean()),
        "real_median_bp": float(sub["actual_adverse_bp"].median()),
    }

def update_directional_slippage_model(model: Dict[str, Any], observation: Dict[str, Any]) -> Dict[str, Any]:
    out = json.loads(json.dumps(model))
    direction = str(observation.get("direction") or order_direction(observation.get("strategy_side") or observation.get("side"), observation.get("order_action") or observation.get("action")))
    models = out.setdefault("models", {})
    m = models.get(direction) or {"points":[]}
    point = {f: float(observation.get(f, 0.0) or 0.0) for f in set(_LINEAR_FEATURES + _KNN_FEATURES)}
    point["actual_adverse_bp"] = float(observation.get("actual_adverse_bp", 0.0) or 0.0)
    point["is_open"] = float(observation.get("is_open", 1.0 if str(observation.get("order_action") or observation.get("action","OPEN")).upper()=="OPEN" else 0.0))
    pts = list(m.get("points") or [])
    pts.append(point)
    pts = pts[-8000:]
    models[direction] = _rebuild_direction_model(pts, direction)
    out["models"] = models
    return out

def predict_directional_slippage_bp(model: Dict[str, Any], row: Dict[str, Any], side: str, action: str, qty: float) -> float:
    if str((model or {}).get("kind","")) not in {"directional_knn_linear","directional_knn_linear_v2"}:
        return max(0.0, float((model or {}).get("base_bp", 0.0)))
    features = make_feature_row(row, side, action, qty)
    direction = order_direction(side, action)
    m = (model.get("models") or {}).get(direction) or {}
    clip_min = float(model.get("clip_min_bp", 0.0))
    clip_max = float(model.get("clip_max_bp", 1000.0))
    if int(m.get("n", 0) or 0) <= 0 or not m.get("beta"):
        base = max(0.0, float(features.get("spread_bp",0.0)) + 0.5 * float(features.get("est_sweep_slip_bp",0.0)))
        return float(np.clip(base, clip_min, clip_max))
    lin = np.array([1.0] + [float(features.get(f, 0.0)) for f in model.get("linear_features", _LINEAR_FEATURES)], dtype=float)
    beta = np.array(m["beta"], dtype=float)
    pred_linear = float(lin @ beta)

    points = m.get("points") or []
    if not points:
        return float(np.clip(pred_linear, clip_min, clip_max))
    means = m.get("scaler_mean") or {}
    stds = m.get("scaler_std") or {}
    q05 = m.get("p05") or {}
    q95 = m.get("p95") or {}
    query = np.array([(float(features.get(f,0.0)) - float(means.get(f,0.0))) / max(float(stds.get(f,1.0)), 1e-6) for f in model.get("knn_features", _KNN_FEATURES)], dtype=float)
    pts = np.array([[(float(p.get(f,0.0)) - float(means.get(f,0.0))) / max(float(stds.get(f,1.0)), 1e-6) for f in model.get("knn_features", _KNN_FEATURES)] for p in points], dtype=float)
    y = np.array([float(p.get("actual_adverse_bp", 0.0)) for p in points], dtype=float)
    d = np.sqrt(np.sum((pts - query[None,:]) ** 2, axis=1))
    k = min(48, len(d))
    idx = np.argpartition(d, k-1)[:k] if len(d) > k else np.arange(len(d))
    ds = d[idx]
    ys = y[idx]
    weights = 1.0 / np.maximum(ds, 0.20) ** 2
    pred_knn = float(np.sum(weights * ys) / np.sum(weights))

    outside = 0.0
    for f in ("log_participation", "dir_pressure_bp", "log_notional", "spread_bp", "est_sweep_slip_bp"):
        v = float(features.get(f, 0.0))
        lo = float(q05.get(f, v)); hi = float(q95.get(f, v))
        span = max(hi - lo, 1e-6)
        if v < lo:
            outside += (lo - v) / span
        elif v > hi:
            outside += (v - hi) / span
    outside = min(1.0, max(0.0, outside))
    w_linear = 0.20 + 0.70 * outside
    pred = (1.0 - w_linear) * pred_knn + w_linear * pred_linear
    return float(np.clip(pred, clip_min, clip_max))

def load_or_fit_model(model_path: str, session_db_path: str, cache_db_path: Optional[str] = None, symbol: Optional[str] = None) -> Dict[str, Any]:
    try:
        if model_path and sqlite3.connect:  # keep lint quiet
            pass
    except Exception:
        pass
    try:
        with open(model_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        frame = build_training_frame(session_db_path, cache_db_path, symbol=symbol)
        return fit_directional_slippage_model(frame)

def save_model(model_path: str, model: Dict[str, Any]) -> None:
    with open(model_path, "w", encoding="utf-8") as f:
        json.dump(model, f, ensure_ascii=False, indent=2)
