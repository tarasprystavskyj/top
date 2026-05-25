#!/usr/bin/env python3
"""One-leg Telegram wrapper for the real V21 Cryptomine pack strategy.

Telegram signals are external gates: a LONG signal enables only the configured
long V21 class for that symbol, and a SHORT signal enables only the short class.
The opposite leg is not instantiated.
"""
from __future__ import annotations

import copy
import importlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from obw_platform.telegram_signal_tools.regime_off_controller import RegimeOffConfig, RegimeOffController

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover - caller reports this as a config error
    yaml = None


ROOT = Path(__file__).resolve().parents[2]
OBW_PLATFORM = ROOT / "obw_platform"
if str(OBW_PLATFORM) not in sys.path:
    sys.path.insert(0, str(OBW_PLATFORM))

LONG_CLASS_KEY = "strategy_class_long"
SHORT_CLASS_KEY = "strategy_class_short"
LONG_PARAMS_KEY = "strategy_params_long"
SHORT_PARAMS_KEY = "strategy_params_short"


@dataclass
class PositionProxy:
    entry: float
    qty: float


def normalize_side(side: str) -> str:
    s = str(side or "").upper()
    if s in {"LONG", "BUY"}:
        return "LONG"
    if s in {"SHORT", "SELL"}:
        return "SHORT"
    raise ValueError(f"unsupported side for Telegram V21 wrapper: {side!r}")


def side_keys(side: str) -> tuple[str, str]:
    s = normalize_side(side)
    return (LONG_CLASS_KEY, LONG_PARAMS_KEY) if s == "LONG" else (SHORT_CLASS_KEY, SHORT_PARAMS_KEY)


def apply_no_trend_filter_variant(params: Dict[str, Any], *, base_order_pct_eq: float = 5.0) -> Dict[str, Any]:
    """Disable V21's trend-warmup-dependent entry sizing/gate for external signals."""
    out = dict(params)
    out["baseOrderPctEq"] = float(base_order_pct_eq)
    out["useTrendAdaptiveSizing"] = 0.0
    out["entryTrendStrengthMin"] = 0.0
    return out


def load_one_leg_config(
    config_path: str,
    side: str,
    delegated_capital_usdt: float,
    *,
    disable_trend_filter: bool = False,
    base_order_pct_eq: float = 5.0,
    regime_off: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML is required to load the V21 config")
    path = Path(config_path)
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = copy.deepcopy(cfg)
    class_key, params_key = side_keys(side)
    if not cfg.get(class_key):
        raise KeyError(f"{class_key} is missing in {config_path}")
    params = dict(cfg.get(params_key) or {})
    params["equityForSizingUSDT"] = float(delegated_capital_usdt)
    params["baseOrderPctEq"] = float(base_order_pct_eq)
    if disable_trend_filter:
        params = apply_no_trend_filter_variant(params, base_order_pct_eq=base_order_pct_eq)
    cfg[params_key] = params
    cfg["telegram_v21_wrapper"] = {
        "active_side": normalize_side(side),
        "active_strategy_class": cfg[class_key],
        "delegated_capital_usdt": float(delegated_capital_usdt),
        "baseOrderPctEq": float(base_order_pct_eq),
        "disable_trend_filter": bool(disable_trend_filter),
        "trend_filter_disabled_fields": (
            ["useTrendAdaptiveSizing", "entryTrendStrengthMin", "baseOrderPctEq"]
            if disable_trend_filter
            else []
        ),
        "source_config": str(path),
        "opposite_leg_enabled": False,
    }
    if regime_off:
        cfg["telegram_v21_wrapper"]["regime_off"] = dict(regime_off)
    return cfg


def import_by_path(path: str) -> Any:
    module_name, cls_name = str(path).rsplit(".", 1)
    module = importlib.import_module(module_name)
    return getattr(module, cls_name)


def make_strategy(cfg: Dict[str, Any], side: str) -> Any:
    class_key, _params_key = side_keys(side)
    cls = import_by_path(str(cfg[class_key]))
    return cls(cfg)


def make_bar(symbol: str, price: float, ts_utc: Optional[str] = None, high: Optional[float] = None, low: Optional[float] = None) -> Dict[str, Any]:
    ts = ts_utc or datetime.now(timezone.utc).isoformat()
    px = float(price)
    return {
        "symbol": symbol,
        "datetime_utc": ts,
        "open": px,
        "high": float(high if high is not None else px),
        "low": float(low if low is not None else px),
        "close": px,
        "gain_24h_before": 0.0,
        "dp6h": 0.0,
        "vol_surge_mult": 0.0,
        "atr_ratio": 0.0,
    }


def _state_snapshot(strategy: Any, symbol: str) -> Dict[str, Any]:
    st = strategy._get_state(symbol)
    return {
        "pos_size": float(st.pos_size or 0.0),
        "pos_value_usdt": float(st.pos_value_usdt or 0.0),
        "avg_price": st.avg_price,
        "num_fills": int(st.num_fills or 0),
        "last_fill_price": st.last_fill_price,
        "next_level_price": st.next_level_price,
        "lots": [[float(q), float(p)] for q, p in list(st.lots or [])],
        "cycle_base_qty_coin": st.cycle_base_qty_coin,
        "cycle_start_ts": st.cycle_start_ts,
        "last_fill_ts": st.last_fill_ts,
        "trailing_active": bool(st.trailing_active),
        "trailing_ref": st.trailing_ref,
        "tp_levels_done": list(st.tp_levels_done or []),
    }


def _regime_off_config(strategy: Any) -> RegimeOffConfig:
    cfg = getattr(strategy, "cfg", {}) or {}
    section = cfg.get("telegram_v21_wrapper") or {}
    return RegimeOffConfig.from_dict(section.get("regime_off"))


def _regime_off_controller(strategy: Any, symbol: str) -> Optional[RegimeOffController]:
    cfg = _regime_off_config(strategy)
    if not cfg.enabled:
        return None
    controllers = getattr(strategy, "_telegram_regime_off_controllers", None)
    if controllers is None:
        controllers = {}
        setattr(strategy, "_telegram_regime_off_controllers", controllers)
    key = str(symbol)
    ctl = controllers.get(key)
    if ctl is None:
        ctl = RegimeOffController(cfg)
        controllers[key] = ctl
    return ctl


def _flatten_state(strategy: Any, symbol: str) -> None:
    st = strategy._get_state(symbol)
    st.pos_size = 0.0
    st.pos_value_usdt = 0.0
    st.avg_price = None
    st.num_fills = 0
    st.last_fill_price = None
    st.next_level_price = None
    st.lots = []
    st.cycle_base_qty_coin = None
    st.trailing_active = False
    st.trailing_ref = None
    st.tp_levels_done = [False] * len(getattr(strategy, "tp_scale_out_levels", []) or [])


def restore_state(strategy: Any, symbol: str, snapshot: Dict[str, Any]) -> None:
    st = strategy._get_state(symbol)
    st.pos_size = float(snapshot.get("pos_size") or 0.0)
    st.pos_value_usdt = float(snapshot.get("pos_value_usdt") or 0.0)
    st.avg_price = snapshot.get("avg_price")
    st.num_fills = int(snapshot.get("num_fills") or 0)
    st.last_fill_price = snapshot.get("last_fill_price")
    st.next_level_price = snapshot.get("next_level_price")
    st.lots = [(float(q), float(p)) for q, p in (snapshot.get("lots") or [])]
    st.cycle_base_qty_coin = snapshot.get("cycle_base_qty_coin")
    st.cycle_start_ts = snapshot.get("cycle_start_ts")
    st.last_fill_ts = snapshot.get("last_fill_ts")
    st.trailing_active = bool(snapshot.get("trailing_active") or False)
    st.trailing_ref = snapshot.get("trailing_ref")
    st.tp_levels_done = list(snapshot.get("tp_levels_done") or [])


def open_external_signal(strategy: Any, symbol: str, row: Dict[str, Any]) -> Dict[str, Any]:
    """Open the V21 cycle from an external Telegram signal.

    The Telegram signal replaces V21's internal entry trigger, but the real
    V21 strategy owns sizing, TP, next DCA level, and later management.
    """
    price = float(row["close"])
    ctl = _regime_off_controller(strategy, symbol)
    if ctl is not None and normalize_side(getattr(strategy, "SIDE", "LONG")) == "LONG":
        decision = ctl.update(price, signal_fresh=True, has_long_position=False)
        if not decision.allow_new_long:
            raise RuntimeError(f"RegimeOff blocks new LONG entry: {decision.reason}")
    st = strategy._get_state(symbol)
    try:
        strategy._last_atr_ratio = float(row.get("atr_ratio", 0.0) or 0.0)
        strategy._apply_regime_budget(row)
        strategy._update_vol_state(st, price)
        strategy._apply_vol_target(st)
        target_pct = strategy._update_trend(st, row.get("datetime_utc"), price)
    except Exception:
        target_pct = float(getattr(strategy, "base_order_pct_eq", 5.0))
    qty = strategy._calc_base_qty(price, target_pct)
    if not strategy._order_value_ok(price, qty):
        raise RuntimeError(f"V21 base order is below min order for {symbol} at {price}")
    st.reset_pending = False
    st.trailing_active = False
    st.trailing_ref = None
    st.pending_new_entry = None
    st.cycle_base_qty_coin = qty
    st.tp_levels_done = [False] * len(getattr(strategy, "tp_scale_out_levels", []) or [])
    st.pos_value_usdt = qty * price
    st.pos_size = qty
    st.avg_price = price
    st.num_fills = 1
    st.last_fill_price = price
    st.next_level_price = strategy._next_level(price, 1)
    st.lots = [(qty, price)]
    try:
        ts = int(strategy._parse_time(row.get("datetime_utc")).timestamp())
    except Exception:
        ts = 0
    st.cycle_start_ts = ts
    st.last_fill_ts = ts
    strategy._register_order()
    sig = type("TelegramV21EntrySig", (), {
        "side": strategy.SIDE,
        "tp": strategy._entry_tp(price),
        "sl": None,
        "reason": "TelegramExternalSignal",
        "qty": qty,
    })()
    snap = _state_snapshot(strategy, symbol)
    return {
        "side": normalize_side(sig.side),
        "qty": float(sig.qty or snap["pos_size"]),
        "entry_price": float(row["close"]),
        "tp": sig.tp,
        "reason": sig.reason,
        "state": snap,
    }


def manage_existing_position(
    strategy: Any,
    symbol: str,
    row: Dict[str, Any],
    snapshot: Dict[str, Any],
) -> Dict[str, Any]:
    restore_state(strategy, symbol, snapshot)
    before = _state_snapshot(strategy, symbol)
    ctl = _regime_off_controller(strategy, symbol)
    if ctl is not None and normalize_side(getattr(strategy, "SIDE", "LONG")) == "LONG":
        decision = ctl.update(
            float(row["close"]),
            signal_fresh=False,
            has_long_position=float(before.get("pos_size") or 0.0) > 0.0,
        )
        if decision.should_close_long:
            _flatten_state(strategy, symbol)
            after = _state_snapshot(strategy, symbol)
            return {
                "event": {
                    "action": "EXIT",
                    "exit_price": float(row["close"]),
                    "qty_frac": 1.0,
                    "reason": f"RegimeOff:{decision.reason}",
                },
                "state": after,
                "pos_entry": float(before.get("avg_price") or row["close"]),
                "pos_qty": float(before.get("pos_size") or 0.0),
            }
    pos = PositionProxy(entry=float(before.get("avg_price") or row["close"]), qty=float(before.get("pos_size") or 0.0))
    exit_sig = strategy.manage_position(symbol, row, pos)
    after = _state_snapshot(strategy, symbol)
    event = None
    if exit_sig is not None:
        event = {
            "action": str(exit_sig.action),
            "exit_price": float(exit_sig.exit_price),
            "qty_frac": float(getattr(exit_sig, "qty_frac", 1.0) or 1.0),
            "reason": str(getattr(exit_sig, "reason", "") or exit_sig.action),
        }
    elif int(after["num_fills"]) > int(before["num_fills"]):
        event = {
            "action": "DCA",
            "fill_count_before": before["num_fills"],
            "fill_count_after": after["num_fills"],
            "qty_added": max(0.0, float(after["pos_size"]) - float(before["pos_size"])),
            "notional_added": max(0.0, float(after["pos_value_usdt"]) - float(before["pos_value_usdt"])),
            "reason": "V21 manage_position DCA",
        }
    return {"event": event, "state": after, "pos_entry": pos.entry, "pos_qty": pos.qty}


def dumps_state(state: Dict[str, Any]) -> str:
    return json.dumps(state, ensure_ascii=False, separators=(",", ":"))


def loads_state(raw: Any) -> Dict[str, Any]:
    if not raw:
        return {}
    return json.loads(str(raw))
