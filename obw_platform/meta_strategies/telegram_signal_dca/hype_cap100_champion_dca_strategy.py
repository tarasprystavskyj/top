#!/usr/bin/env python3
"""Reusable HYPE cap100 champion DCA policy.

This module is the trader policy for candidate 189. Paper and live wrappers
should consume its plans/intents instead of embedding DCA levels or notionals.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional


CHAMPION_CANDIDATE_INDEX = 189
CHAMPION_PARAMS = {
    "dca_add_mode": "multiplier",
    "dca_add_notional_usdt": 2.0,
    "dca_profile": "default",
    "dca_min_order_usdt": 2.0,
    "fresh_base_pct": 28.0,
    "fresh_callback_percent": 0.45,
    "fresh_tp_percent": 1.4,
    "freshness_ms": 259200000,
    "max_position_cost_pct": 100.0,
    "normal_base_pct": 10.0,
    "tp_freshness_ms": 345600000,
}
DCA_DROPS_PCT = (0.25, 0.35, 0.55, 3.00)
DCA_MULTIPLIERS = (1.0, 1.5, 2.75, 1.5)
V21_STRICT_LONG = {
    "base_order_pct_eq": 1.5,
    "equity_for_sizing_usdt": 208.0,
    "min_order_usdt": 2.0,
    "steps": (0.3, 0.35, 0.6, 0.8, 0.8),
    "multipliers": (1.2, 1.0, 1.5, 3.5),
}
V21_STRICT_SHORT = {
    "base_order_pct_eq": 1.388859,
    "equity_for_sizing_usdt": 214.0,
    "min_order_usdt": 2.0,
    "steps": (0.1, 0.4, 0.6, 0.8, 0.8),
    "multipliers": (2.272696, 1.0, 2.0, 3.5),
}


def dca_levels(entry_price: float, side: str = "LONG") -> List[float]:
    levels: List[float] = []
    last = entry_price
    for drop in DCA_DROPS_PCT:
        last *= 1.0 - drop / 100.0 if str(side).upper() == "LONG" else 1.0 + drop / 100.0
        levels.append(last)
    return levels


def _v21_side_params(side: str) -> Dict[str, Any]:
    return V21_STRICT_LONG if str(side).upper() == "LONG" else V21_STRICT_SHORT


def _v21_selected_dca_count(sizing: Optional[Dict[str, Any]]) -> int:
    if not isinstance(sizing, dict):
        return -1
    raw = sizing.get("selected_dca_count")
    if raw is None:
        raw = sizing.get("dca_count")
    if raw is None:
        profile = str(sizing.get("dca_profile") or "")
        if "dca" in profile.lower():
            tail = profile.lower().rsplit("dca", 1)[-1]
            digits = "".join(ch for ch in tail if ch.isdigit())
            raw = digits if digits else None
    try:
        return max(0, int(raw))
    except Exception:
        return -1


def _build_v21_same_max_plan_for_target(target: float, entry_price: float, *, side: str, sizing: Dict[str, Any]) -> Dict[str, Any]:
    target = max(float(target or 0.0), 0.0)
    dca_count = min(_v21_selected_dca_count(sizing), 5)
    params = _v21_side_params(side)
    min_order = float(params["min_order_usdt"])
    raw_base = max(min_order, float(params["equity_for_sizing_usdt"]) * float(params["base_order_pct_eq"]) / 100.0)
    steps = [float(x) for x in params["steps"][:dca_count]]
    multipliers = [float(x) for x in params["multipliers"][:dca_count]]
    if dca_count <= 0:
        base = target
        adds: List[float] = []
    else:
        raw_adds = [max(min_order, raw_base * mult) for mult in multipliers]
        planned = raw_base + sum(raw_adds)
        scale = target / max(planned, 1e-12)
        base = raw_base * scale
        adds = [x * scale for x in raw_adds]
    levels: List[float] = []
    last = entry_price
    for step in steps:
        last *= 1.0 - step / 100.0 if str(side).upper() == "LONG" else 1.0 + step / 100.0
        levels.append(last)
    return {
        "target_notional": target,
        "base_notional": base,
        "add_notionals": adds,
        "dca_add_mode": "v21_same_max",
        "min_order_notional": min_order,
        "levels": levels,
        "candidate_index": CHAMPION_CANDIDATE_INDEX,
        "box_config_class": "V21StrictTrendStableBoxConfig",
        "dca_profile": sizing.get("dca_profile") or f"v21_same_max_dca{dca_count}",
        "selected_dca_count": dca_count,
        "base_order_policy": sizing.get("base_order_policy"),
    }


def build_plan_for_target(target: float, entry_price: float, side: str = "LONG", sizing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if isinstance(sizing, dict) and str(sizing.get("dca_profile") or "").startswith("v21_same_max"):
        return _build_v21_same_max_plan_for_target(target, entry_price, side=side, sizing=sizing)
    target = max(float(target or 0.0), 0.0)
    base = min(target * CHAMPION_PARAMS["fresh_base_pct"] / 100.0, target)
    remaining = max(target - base, 0.0)
    dca_mode = str(CHAMPION_PARAMS.get("dca_add_mode") or "multiplier").strip().lower()
    if dca_mode == "fixed":
        fixed = max(0.0, float(CHAMPION_PARAMS.get("dca_add_notional_usdt") or 0.0))
        raw_adds = [fixed for _ in DCA_MULTIPLIERS]
    elif dca_mode == "min_order":
        min_order = max(0.0, float(CHAMPION_PARAMS.get("dca_min_order_usdt") or 0.0))
        raw_adds = [min_order for _ in DCA_MULTIPLIERS]
    else:
        dca_mode = "multiplier"
        raw_adds = [base * m for m in DCA_MULTIPLIERS]
    scale = min(1.0, remaining / max(sum(raw_adds), 1e-12))
    add_notionals = [x * scale for x in raw_adds]
    return {
        "target_notional": target,
        "base_notional": base,
        "add_notionals": add_notionals,
        "dca_add_mode": dca_mode,
        "min_order_notional": 0.0,
        "levels": dca_levels(entry_price, side),
        "candidate_index": CHAMPION_CANDIDATE_INDEX,
        "box_config_class": "Candidate189BoxConfig",
    }


def build_plan(equity: float, args: Any, entry_price: float, side: str = "LONG", sizing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    target = min(float(args.initial_target_notional), float(args.max_gross_notional_usdt), max(equity, 0.0))
    return build_plan_for_target(target, entry_price, side=side, sizing=sizing)


def resize_trade_plan(
    trade: Dict[str, Any],
    target: float,
    *,
    now: datetime,
    iso_fn,
    reason: str,
    basis: str,
    sizing: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    old_target = float(trade.get("target_notional") or 0.0)
    new_target = max(float(target or 0.0), 0.0)
    if new_target <= 0:
        return None
    if old_target > 0 and abs(new_target - old_target) <= max(old_target, 1.0) * 1e-9:
        return None
    entry = float(trade.get("lead_entry_price") or trade.get("avg_entry") or 0.0)
    if entry <= 0:
        return None
    plan = build_plan_for_target(new_target, entry, side=str(trade.get("side") or "LONG"), sizing=sizing or trade.get("strategy_sizing"))
    initial_target = float(trade.get("source_box_initial_target_notional") or old_target or new_target)
    trade["source_box_initial_target_notional"] = initial_target
    trade["source_box_previous_target_notional"] = old_target
    trade["source_box_current_target_notional"] = new_target
    trade["source_box_ratio"] = new_target / max(initial_target, 1e-12)
    trade["source_box_target_basis"] = basis
    trade["source_box_last_resize_utc"] = iso_fn(now)
    trade["target_notional"] = plan["target_notional"]
    trade["base_notional"] = plan["base_notional"]
    trade["add_notionals"] = plan["add_notionals"]
    trade["levels"] = plan["levels"]
    trade["dca_add_mode"] = plan["dca_add_mode"]
    trade["min_order_notional"] = plan["min_order_notional"]
    trade["box_config_class"] = plan.get("box_config_class")
    trade["dca_profile"] = plan.get("dca_profile")
    trade["selected_dca_count"] = plan.get("selected_dca_count")
    trade["base_order_policy"] = plan.get("base_order_policy")
    return {
        "type": "source_box_resized",
        "key": trade.get("key"),
        "utc": iso_fn(now),
        "reason": reason,
        "basis": basis,
        "old_target_notional": old_target,
        "new_target_notional": new_target,
        "source_box_ratio": trade["source_box_ratio"],
        "next_level_idx": trade.get("next_level_idx"),
        "base_notional": trade.get("base_notional"),
        "add_notionals": list(trade.get("add_notionals") or []),
        "box_config_class": trade.get("box_config_class"),
        "dca_profile": trade.get("dca_profile"),
        "selected_dca_count": trade.get("selected_dca_count"),
    }


def build_trade_from_source(pos: Dict[str, Any], plan: Dict[str, Any], *, now: datetime, mark: Optional[float], iso_fn) -> Dict[str, Any]:
    side = str(pos["side"])
    entry = float(pos["entry_price"])
    source_leverage_raw = pos.get("leverage")
    try:
        source_leverage = float(source_leverage_raw)
    except Exception:
        source_leverage = None
    if source_leverage is not None and source_leverage <= 0:
        source_leverage = None
    source_margin_mode = str(pos.get("isolated") or pos.get("margin_mode") or "").strip()
    try:
        source_position_amount_abs = abs(float(pos.get("position_amount") or 0.0))
    except Exception:
        source_position_amount_abs = 0.0
    try:
        source_notional_value_abs = abs(float(pos.get("notional_value") or 0.0))
    except Exception:
        source_notional_value_abs = 0.0
    return {
        "key": f"{pos['symbol']}:{side}",
        "lead_position_id": pos.get("id"),
        "symbol": pos["symbol"],
        "side": side,
        "source_leverage_raw": source_leverage_raw,
        "source_leverage": source_leverage,
        "source_margin_mode": source_margin_mode,
        "source_position_amount_abs": source_position_amount_abs,
        "source_notional_value_abs": source_notional_value_abs,
        "lead_margin_balance_usdt": pos.get("lead_margin_balance_usdt"),
        "source_position_margin_usdt": pos.get("source_position_margin_usdt"),
        "source_position_margin_source": pos.get("source_position_margin_source"),
        "source_margin_fraction": pos.get("source_margin_fraction"),
        "source_margin_fraction_reason": pos.get("source_margin_fraction_reason"),
        "source_size_last_sync_utc": iso_fn(now),
        "opened_at_utc": iso_fn(now),
        "detected_at_ms": int(now.timestamp() * 1000),
        "lead_entry_price": entry,
        "target_notional": plan["target_notional"],
        "base_notional": plan["base_notional"],
        "add_notionals": plan["add_notionals"],
        "levels": plan["levels"],
        "strategy_sizing": pos.get("strategy_sizing"),
        "strategy_config_source": pos.get("strategy_config_source"),
        "box_config_class": plan.get("box_config_class"),
        "dca_profile": plan.get("dca_profile"),
        "selected_dca_count": plan.get("selected_dca_count"),
        "base_order_policy": plan.get("base_order_policy"),
        "next_level_idx": 0,
        "qty": 0.0,
        "notional": 0.0,
        "avg_entry": 0.0,
        "fees_paid": 0.0,
        "last_mark": mark,
        "last_seen_utc": iso_fn(now),
        "strategy_candidate_index": CHAMPION_CANDIDATE_INDEX,
    }


def base_entry_intent(trade: Dict[str, Any], *, expected_price: float) -> Dict[str, Any]:
    return {
        "intent_type": "open_entry",
        "action": "OPEN",
        "key": trade.get("key"),
        "trade": trade,
        "expected_price": expected_price,
        "notional": float(trade["base_notional"]),
        "fill_type": "base_entry",
        "reason": "lead_open_position_detected",
        "strategy_policy": "copy_source_open_base_entry",
        "candidate_index": CHAMPION_CANDIDATE_INDEX,
    }


def dca_entry_intents(trade: Dict[str, Any], *, mark: Optional[float], allow_dca: bool) -> List[Dict[str, Any]]:
    if not allow_dca:
        return []
    intents: List[Dict[str, Any]] = []
    idx = int(trade.get("next_level_idx") or 0)
    levels = list(trade.get("levels") or [])
    add_notionals = list(trade.get("add_notionals") or [])
    while idx < len(levels):
        level = float(levels[idx])
        side = str(trade.get("side") or "LONG").upper()
        if mark is None:
            break
        if side == "LONG" and mark > level:
            break
        if side == "SHORT" and mark < level:
            break
        intents.append(
            {
                "intent_type": "dca_entry",
                "action": "OPEN",
                "key": trade.get("key"),
                "trade": trade,
                "expected_price": level,
                "notional": float(add_notionals[idx]),
                "fill_type": f"dca_add_{idx + 1}",
                "reason": "mark_crossed_dca_level",
                "strategy_policy": "candidate_189_dca_level",
                "candidate_index": CHAMPION_CANDIDATE_INDEX,
                "level_idx": idx,
            }
        )
        idx += 1
    return intents
