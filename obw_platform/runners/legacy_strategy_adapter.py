from __future__ import annotations

import inspect
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class LegacyStrategyPolicy:
    enabled: bool = False
    legacy_runner_notional_sizing: bool = False
    legacy_dca_order_type: str = ""


def legacy_policy_from_config(cfg: Optional[Dict[str, Any]]) -> LegacyStrategyPolicy:
    cfg = cfg or {}
    runner = cfg.get("runner") or {}
    legacy = cfg.get("legacy_strategy_adapter") or runner.get("legacy_strategy_adapter") or {}
    enabled = bool(legacy.get("enabled", cfg.get("legacy_strategy_compat", runner.get("legacy_strategy_compat", False))))
    return LegacyStrategyPolicy(
        enabled=enabled,
        legacy_runner_notional_sizing=bool(legacy.get("legacy_runner_notional_sizing", False)),
        legacy_dca_order_type=str(legacy.get("legacy_dca_order_type", legacy.get("dca_order_type", "")) or "").lower().strip(),
    )


def _sig_get(sig: Any, key: str, default=None):
    try:
        if hasattr(sig, key):
            return getattr(sig, key)
    except Exception:
        pass
    try:
        if isinstance(sig, dict) and key in sig:
            return sig.get(key, default)
    except Exception:
        pass
    return default


def normalize_legacy_entry_signal(signal: Any, policy: LegacyStrategyPolicy) -> Any:
    if signal is None or not policy.enabled:
        return signal
    qty = _sig_get(signal, "qty")
    size = _sig_get(signal, "size")
    sizing_policy = _sig_get(signal, "sizing_policy", _sig_get(signal, "sizingPolicy"))
    updates = {}
    if (qty is None or qty == "") and size not in (None, ""):
        updates["qty"] = size
    if (qty is None or qty == "") and size in (None, "") and not sizing_policy and policy.legacy_runner_notional_sizing:
        updates["sizing_policy"] = "delegate_notional"
    if not updates:
        return signal
    if isinstance(signal, dict):
        out = dict(signal)
        out.update(updates)
        return out
    data = {}
    try:
        data.update(vars(signal))
    except Exception:
        pass
    data.update(updates)
    for key in ("tp", "sl", "reason", "order_type", "limit_price", "entry_limit_post_only"):
        val = _sig_get(signal, key, None)
        if val is not None and key not in data:
            data[key] = val
    return SimpleNamespace(**data)


def call_manage_position(strat: Any, sym: str, row: dict, pos: Any, ctx: dict, policy: LegacyStrategyPolicy) -> Any:
    fn = getattr(strat, "manage_position")
    if not policy.enabled:
        return fn(sym, row, pos, ctx=ctx)
    try:
        sig = inspect.signature(fn)
        sig.bind(sym, row, pos, ctx=ctx)
    except ValueError:
        return fn(sym, row, pos, ctx=ctx)
    except TypeError as exc:
        try:
            sig.bind(True, sym, row, pos, ctx)
        except TypeError:
            raise exc
        return fn(True, sym, row, pos, ctx)
    return fn(sym, row, pos, ctx=ctx)


def legacy_execution_policy(policy: LegacyStrategyPolicy, event: str) -> dict:
    if not policy.enabled:
        return {}
    if str(event or "").lower() == "dca" and policy.legacy_dca_order_type:
        return {"order_type": policy.legacy_dca_order_type}
    return {}
