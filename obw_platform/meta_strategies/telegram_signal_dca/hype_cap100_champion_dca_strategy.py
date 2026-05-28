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
    "min_order_qty_hype": 0.105,
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


def dca_levels(entry_price: float) -> List[float]:
    levels: List[float] = []
    last = entry_price
    for drop in DCA_DROPS_PCT:
        last *= 1.0 - drop / 100.0
        levels.append(last)
    return levels


def build_plan(equity: float, args: Any, entry_price: float) -> Dict[str, Any]:
    target = min(float(args.initial_target_notional), float(args.max_gross_notional_usdt), max(equity, 0.0))
    base = min(target * CHAMPION_PARAMS["fresh_base_pct"] / 100.0, target)
    min_order_notional = max(
        0.0,
        float(CHAMPION_PARAMS.get("dca_min_order_usdt") or 0.0),
        float(CHAMPION_PARAMS.get("min_order_qty_hype") or 0.0) * max(float(entry_price or 0.0), 0.0),
    )
    if min_order_notional > 0:
        base = min(target, max(base, min_order_notional))
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
    if min_order_notional > 0:
        add_notionals = [max(x, min_order_notional) if x > 0 else 0.0 for x in add_notionals]
    return {
        "target_notional": target,
        "base_notional": base,
        "add_notionals": add_notionals,
        "dca_add_mode": dca_mode,
        "min_order_notional": min_order_notional,
        "min_order_qty_hype": float(CHAMPION_PARAMS.get("min_order_qty_hype") or 0.0),
        "levels": dca_levels(entry_price),
        "candidate_index": CHAMPION_CANDIDATE_INDEX,
    }


def build_trade_from_source(pos: Dict[str, Any], plan: Dict[str, Any], *, now: datetime, mark: Optional[float], iso_fn) -> Dict[str, Any]:
    side = str(pos["side"])
    entry = float(pos["entry_price"])
    return {
        "key": f"{pos['symbol']}:{side}",
        "lead_position_id": pos.get("id"),
        "symbol": pos["symbol"],
        "side": side,
        "opened_at_utc": iso_fn(now),
        "detected_at_ms": int(now.timestamp() * 1000),
        "lead_entry_price": entry,
        "target_notional": plan["target_notional"],
        "base_notional": plan["base_notional"],
        "add_notionals": plan["add_notionals"],
        "levels": plan["levels"],
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
        if mark is None or mark > level:
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
