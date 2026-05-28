from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


INTENT_HOLD = "HOLD"
INTENT_OPEN = "OPEN"
INTENT_ADD = "ADD"
INTENT_CLOSE = "CLOSE"
INTENT_PARTIAL_CLOSE = "PARTIAL_CLOSE"
INTENT_SYNC_STATE = "SYNC_STATE"


@dataclass(frozen=True)
class ExecutionIntent:
    kind: str
    symbol: str
    side: str
    qty: float = 0.0
    qty_frac: float = 0.0
    reason: str = ""
    order_type: str = "market"
    limit_price: Optional[float] = None
    post_only: bool = True
    allow_market_fallback: bool = False
    fallback_on_reject: bool = False
    fallback_on_timeout: bool = False
    sizing_policy: str = "explicit_qty"
    close_semantics: str = ""
    tp_price: Optional[float] = None
    sl_price: Optional[float] = None
    source: str = ""
    raw: Any = None
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_trade(self) -> bool:
        return self.kind in {INTENT_OPEN, INTENT_ADD, INTENT_CLOSE, INTENT_PARTIAL_CLOSE}


class StrategyRejectionBackoff:
    """Strategy-owned rejection circuit.

    The live runner may consult this object, but the state and policy belong to
    the strategy. This keeps retry-storm prevention visible to strategy state
    instead of hiding it in the execution adapter.
    """

    def __init__(self, *, max_failures: int = 3, cooldown_sec: float = 60.0):
        self.max_failures = int(max_failures)
        self.cooldown_sec = float(cooldown_sec)
        self.failures = 0
        self.blocked_until = 0.0
        self.last_event = ""
        self.last_details: Any = None

    def can_submit(self, now: Optional[float] = None) -> bool:
        now_f = time.time() if now is None else float(now)
        return now_f >= float(self.blocked_until or 0.0)

    def record_rejection(self, event: str = "", details: Any = None, now: Optional[float] = None) -> None:
        now_f = time.time() if now is None else float(now)
        self.failures += 1
        self.last_event = str(event or "")
        self.last_details = details
        if self.max_failures > 0 and self.failures >= self.max_failures:
            self.blocked_until = max(float(self.blocked_until or 0.0), now_f + max(0.0, self.cooldown_sec))

    def record_fill(self, event: str = "") -> None:
        self.failures = 0
        self.blocked_until = 0.0

    def export_state(self) -> Dict[str, Any]:
        return {
            "max_failures": self.max_failures,
            "cooldown_sec": self.cooldown_sec,
            "failures": self.failures,
            "blocked_until": self.blocked_until,
            "last_event": self.last_event,
            "last_details": self.last_details,
        }


def sig_get(sig: Any, key: str, default: Any = None) -> Any:
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


def normalize_reason(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    try:
        text = str(value).strip()
    except Exception:
        text = f"{value}"
    return fallback if not text or text.lower() in {"", "nan", "none", "null", "nat"} else text


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def entry_intent_from_signal(
    *,
    symbol: str,
    side: str,
    signal: Any,
    requested_price: float,
    notional_long: float,
    notional_short: float,
) -> ExecutionIntent:
    if signal is None:
        return ExecutionIntent(kind=INTENT_HOLD, symbol=symbol, side=side, source="entry_signal")
    qty = _safe_float(sig_get(signal, "qty"))
    sizing_policy = str(sig_get(signal, "sizing_policy", sig_get(signal, "sizingPolicy", "explicit_qty")) or "explicit_qty").lower().strip()
    if qty is None or qty <= 0:
        if sizing_policy not in {"delegate_notional", "runner_notional"}:
            return ExecutionIntent(
                kind=INTENT_HOLD,
                symbol=symbol,
                side=str(side).upper(),
                reason="missing_explicit_qty",
                source="entry_signal",
                raw=signal,
                extra={"rejected_contract": "entry_signal_missing_qty_or_sizing_policy"},
            )
        notional = _safe_float(sig_get(signal, "notional", sig_get(signal, "notional_usdt", None)))
        if notional is None or notional <= 0:
            notional = float(notional_long if str(side).upper() == "LONG" else notional_short)
        qty = notional / max(float(requested_price or 0.0), 1e-12)
    order_type = str(sig_get(signal, "order_type", "market") or "market").lower().strip()
    limit_price = sig_get(signal, "limit_price", None)
    if limit_price in (None, "") and order_type in {"limit", "maker", "maker_limit"}:
        limit_price = requested_price
    return ExecutionIntent(
        kind=INTENT_OPEN,
        symbol=symbol,
        side=str(side).upper(),
        qty=float(qty or 0.0),
        reason=normalize_reason(sig_get(signal, "reason", None), "entry"),
        order_type=order_type,
        limit_price=_safe_float(limit_price),
        post_only=bool(sig_get(signal, "entry_limit_post_only", True)),
        allow_market_fallback=bool(sig_get(signal, "allow_market_fallback", False)),
        fallback_on_reject=bool(sig_get(signal, "fallback_on_reject", sig_get(signal, "market_fallback_on_reject", False))),
        fallback_on_timeout=bool(sig_get(signal, "fallback_on_timeout", sig_get(signal, "market_fallback_on_timeout", False))),
        sizing_policy=sizing_policy,
        tp_price=_safe_float(sig_get(signal, "tp", sig_get(signal, "take_profit", None))),
        sl_price=_safe_float(sig_get(signal, "sl", sig_get(signal, "stop_price", None))),
        source="entry_signal",
        raw=signal,
    )


def manage_intent_from_result(
    *,
    symbol: str,
    side: str,
    result: Any,
    qty_before: float,
    entry_before: float,
    qty_after: Optional[float] = None,
    entry_after: Optional[float] = None,
    default_order_type: Optional[str] = None,
    default_limit_price: Optional[float] = None,
) -> ExecutionIntent:
    action = str(sig_get(result, "action", "") or "").upper()
    reason = normalize_reason(sig_get(result, "reason", None), action.lower())
    side_u = str(side).upper()
    if action in {"TP", "SL", "EXIT", "CLOSE"}:
        return ExecutionIntent(kind=INTENT_CLOSE, symbol=symbol, side=side_u, qty=float(qty_before or 0.0), reason=reason, close_semantics=action.lower(), source="manage_position", raw=result)
    if action in {"TP_PARTIAL", "PARTIAL", "PARTIAL_CLOSE"}:
        frac = max(0.0, min(1.0, float(sig_get(result, "qty_frac", 0.0) or 0.0)))
        return ExecutionIntent(kind=INTENT_PARTIAL_CLOSE, symbol=symbol, side=side_u, qty=float(qty_before or 0.0) * frac, qty_frac=frac, reason=reason or action.lower(), close_semantics=action.lower(), source="manage_position", raw=result)
    if action in {"ADD", "DCA", "OPEN_MORE"}:
        delta = _safe_float(sig_get(result, "delta_qty", sig_get(result, "qty", None)), 0.0) or 0.0
        order_type = str(sig_get(result, "order_type", default_order_type or "") or "").lower().strip()
        if delta > 0 and not order_type:
            return ExecutionIntent(kind=INTENT_HOLD, symbol=symbol, side=side_u, reason="missing_dca_order_style", source="manage_position", raw=result, extra={"rejected_contract": "dca_missing_order_type"})
        return ExecutionIntent(kind=INTENT_ADD if delta > 0 else INTENT_HOLD, symbol=symbol, side=side_u, qty=float(delta), reason=reason or action.lower(), order_type=order_type or "market", limit_price=_safe_float(sig_get(result, "limit_price", default_limit_price)), source="manage_position", raw=result)
    if qty_after is not None and float(qty_after) > float(qty_before or 0.0) + 1e-12:
        order_type = str(default_order_type or "").lower().strip()
        if not order_type:
            return ExecutionIntent(kind=INTENT_HOLD, symbol=symbol, side=side_u, reason="missing_dca_order_style", source="manage_position_state_delta", raw=result, extra={"rejected_contract": "state_delta_dca_missing_order_type"})
        return ExecutionIntent(kind=INTENT_ADD, symbol=symbol, side=side_u, qty=float(qty_after) - float(qty_before or 0.0), reason="strategy_state_dca", order_type=order_type, limit_price=_safe_float(default_limit_price), source="manage_position_state_delta", raw=result)
    if entry_after is not None and abs(float(entry_after) - float(entry_before or 0.0)) > 1e-12:
        return ExecutionIntent(kind=INTENT_SYNC_STATE, symbol=symbol, side=side_u, reason="strategy_state_entry_sync", source="manage_position_state_delta", raw=result)
    return ExecutionIntent(kind=INTENT_HOLD, symbol=symbol, side=side_u, source="manage_position", raw=result)


def _strategy_backoff(strat: Any, sym: str) -> Any:
    getter = getattr(strat, "get_execution_backoff", None)
    if callable(getter):
        try:
            return getter(sym)
        except Exception:
            return None
    return getattr(strat, "execution_backoff", None)


def strategy_can_submit(strat: Any, sym: str, event: str, details: Any = None) -> bool:
    fn = getattr(strat, "can_submit_order", None)
    if callable(fn):
        try:
            if not bool(fn(sym, event=event, details=details)):
                return False
        except Exception:
            return False
    backoff = _strategy_backoff(strat, sym)
    can = getattr(backoff, "can_submit", None)
    if callable(can):
        try:
            return bool(can())
        except Exception:
            return False
    return True


def notify_strategy_rejected(strat: Any, sym: str, event: str, details: Any = None) -> None:
    fn = getattr(strat, "on_order_rejected", None)
    if callable(fn):
        try:
            fn(sym, event=event, details=details)
        except Exception:
            pass
    backoff = _strategy_backoff(strat, sym)
    rec = getattr(backoff, "record_rejection", None)
    if callable(rec):
        try:
            rec(event=event, details=details)
        except Exception:
            pass


def notify_strategy_filled(strat: Any, sym: str, event: str = "open") -> None:
    event_norm = str(event or "").lower()
    if event_norm in {"close", "partial", "partial_close", "sync_absent", "sync_reconcile"}:
        return
    backoff = _strategy_backoff(strat, sym)
    rec = getattr(backoff, "record_fill", None)
    if callable(rec):
        try:
            rec(event=event_norm)
        except TypeError:
            rec()
        except Exception:
            pass
