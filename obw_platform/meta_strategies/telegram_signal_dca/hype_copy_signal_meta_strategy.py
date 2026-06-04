#!/usr/bin/env python3
"""Copy-source meta-strategy for HYPE cap100.

This module interprets source/Binance facts and emits explicit strategy
intents. Execution wrappers should submit orders only from these intents.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    from . import hype_cap100_champion_dca_strategy as dca
except ImportError:  # pragma: no cover - script import path
    import hype_cap100_champion_dca_strategy as dca


SOURCE_HISTORY_OPEN_TOLERANCE_SEC = 2 * 60 * 60
SOURCE_HISTORY_CLOSE_TOLERANCE_SEC = 30 * 60


def parse_iso_dt(raw: Any) -> Optional[datetime]:
    if raw in (None, ""):
        return None
    try:
        text = str(raw)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def ms_to_dt(raw: Any) -> Optional[datetime]:
    try:
        ms = int(raw or 0)
    except Exception:
        return None
    if ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def time_lag_sec(a: Optional[datetime], b: Optional[datetime]) -> Optional[float]:
    if not a or not b:
        return None
    return abs((a - b).total_seconds())


def source_history_open_dt(row: Dict[str, Any]) -> Optional[datetime]:
    return parse_iso_dt(row.get("opened_utc")) or ms_to_dt(row.get("opened_ms"))


def source_history_close_dt(row: Dict[str, Any]) -> Optional[datetime]:
    return parse_iso_dt(row.get("closed_utc")) or ms_to_dt(row.get("closed_ms"))


def validate_source_history_match(trade: Dict[str, Any], row: Optional[Dict[str, Any]], close_time: datetime, *, iso_fn) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    if not row:
        return None, {"valid": False, "reason": "missing_history"}
    key = str(trade.get("key") or "")
    if row.get("key") and str(row.get("key")) != key:
        return None, {"valid": False, "reason": "key_mismatch", "history_key": row.get("key"), "trade_key": key}
    lead_id = str(trade.get("lead_position_id") or "")
    hist_id = str(row.get("id") or "")
    if lead_id and hist_id and lead_id != hist_id:
        return None, {"valid": False, "reason": "position_id_mismatch", "history_id": hist_id, "lead_position_id": lead_id}
    opened_ref = ms_to_dt(trade.get("detected_at_ms")) or parse_iso_dt(trade.get("opened_at_utc"))
    hist_open = source_history_open_dt(row)
    open_lag = time_lag_sec(opened_ref, hist_open)
    if open_lag is None:
        return None, {"valid": False, "reason": "missing_open_time", "history_opened_utc": row.get("opened_utc"), "trade_opened_at_utc": trade.get("opened_at_utc")}
    if open_lag > SOURCE_HISTORY_OPEN_TOLERANCE_SEC:
        return None, {"valid": False, "reason": "open_time_mismatch", "open_lag_sec": open_lag, "history_opened_utc": row.get("opened_utc"), "trade_opened_at_utc": trade.get("opened_at_utc")}
    hist_close = source_history_close_dt(row)
    close_lag = time_lag_sec(close_time, hist_close)
    if close_lag is None:
        return None, {"valid": False, "reason": "missing_close_time", "history_closed_utc": row.get("closed_utc"), "close_event_utc": iso_fn(close_time)}
    if close_lag > SOURCE_HISTORY_CLOSE_TOLERANCE_SEC:
        return None, {"valid": False, "reason": "close_time_mismatch", "close_lag_sec": close_lag, "history_closed_utc": row.get("closed_utc"), "close_event_utc": iso_fn(close_time)}
    if not row.get("avg_close_price"):
        return None, {"valid": False, "reason": "missing_avg_close_price"}
    return row, {"valid": True, "open_lag_sec": open_lag, "close_lag_sec": close_lag}


def find_valid_source_history_exit(trade: Dict[str, Any], history: List[Dict[str, Any]], close_time: datetime, *, iso_fn) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    candidates = [row for row in (history or []) if row.get("key") == trade.get("key") and row.get("avg_close_price")]
    lead_id = str(trade.get("lead_position_id") or "")
    if lead_id:
        exact = [row for row in candidates if str(row.get("id") or "") == lead_id]
        if exact:
            candidates = exact
    ranked = sorted(candidates, key=lambda row: (time_lag_sec(close_time, source_history_close_dt(row)) if source_history_close_dt(row) else float("inf")))
    rejects = []
    for row in ranked:
        valid, meta = validate_source_history_match(trade, row, close_time, iso_fn=iso_fn)
        if valid:
            return valid, {**meta, "candidate_count": len(candidates)}
        rejects.append(meta)
    return None, {"valid": False, "reason": "no_valid_history_match", "candidate_count": len(candidates), "rejects": rejects[:5]}


def source_key(pos: Dict[str, Any]) -> str:
    return f"{pos['symbol']}:{pos['side']}"


def _normalize_symbol(raw: Any) -> str:
    text = str(raw or "").upper().strip()
    if "/" in text:
        base = text.split("/", 1)[0]
        quote = text.split("/", 1)[1].split(":", 1)[0]
        return f"{base}{quote}"
    return text.replace("-", "").replace(":", "")


def _symbol_filter(raw: Any) -> Optional[Set[str]]:
    text = str(raw or "").upper().strip()
    if text in {"", "*", "ALL", "ANY", "MULTI", "MULTI_SYMBOL"}:
        return None
    return {_normalize_symbol(part) for part in text.split(",") if _normalize_symbol(part)}


def mark_for_symbol(args: Any, default_mark: Optional[float], symbol: Any) -> Optional[float]:
    marks = getattr(args, "_symbol_marks", None)
    if isinstance(marks, dict):
        key = _normalize_symbol(symbol)
        value = marks.get(key)
        try:
            out = float(value)
        except Exception:
            out = 0.0
        if out > 0:
            return out
    return default_mark


def filter_source_positions(positions: List[Dict[str, Any]], *, symbol: str, long_only: bool) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    allowed = _symbol_filter(symbol)
    filtered: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []
    for pos in positions:
        pos_symbol = _normalize_symbol(pos.get("symbol"))
        if allowed is not None and pos_symbol not in allowed:
            continue
        if str(pos.get("side")) == "SHORT" and long_only:
            events.append({"type": "signal_ignored", "reason": "long_only", "key": pos.get("key")})
            continue
        filtered.append(pos)
    return {source_key(p): p for p in filtered}, events


def build_strategy_intents(
    state: Dict[str, Any],
    positions: List[Dict[str, Any]],
    history: List[Dict[str, Any]],
    mark: Optional[float],
    now: datetime,
    args: Any,
    *,
    allow_dca: bool,
    iso_fn,
) -> Dict[str, Any]:
    current, events = filter_source_positions(positions, symbol=args.symbol, long_only=bool(args.long_only))
    open_trades: Dict[str, Dict[str, Any]] = state.setdefault("open_trades", {})
    intents: List[Dict[str, Any]] = []

    for key, pos in sorted(current.items()):
        pos_mark = mark_for_symbol(args, mark, pos.get("symbol"))
        if key not in open_trades:
            entry = float(pos["entry_price"])
            plan = dca.build_plan(float(state.get("equity") or args.initial_equity), args, entry)
            trade = dca.build_trade_from_source(pos, plan, now=now, mark=pos_mark, iso_fn=iso_fn)
            intents.append(dca.base_entry_intent(trade, expected_price=entry))
            intents.extend(dca.dca_entry_intents(trade, mark=pos_mark, allow_dca=allow_dca))
        else:
            trade = open_trades[key]
            trade["last_seen_utc"] = iso_fn(now)
            trade["last_mark"] = pos_mark
            trade["source_leverage_raw"] = pos.get("leverage")
            try:
                source_leverage = float(pos.get("leverage"))
            except Exception:
                source_leverage = None
            trade["source_leverage"] = source_leverage if source_leverage and source_leverage > 0 else None
            trade["source_margin_mode"] = str(pos.get("isolated") or pos.get("margin_mode") or trade.get("source_margin_mode") or "").strip()
            intents.extend(dca.dca_entry_intents(trade, mark=pos_mark, allow_dca=allow_dca))

    keys_to_close = set(open_trades) - set(current)
    for key in sorted(keys_to_close):
        trade = open_trades[key]
        trade_mark = mark_for_symbol(args, mark, trade.get("symbol"))
        hist, hist_meta = find_valid_source_history_exit(trade, history, now, iso_fn=iso_fn)
        if hist and hist.get("avg_close_price"):
            exit_price = float(hist["avg_close_price"])
            reason = "position_history_closed"
        else:
            exit_price = float(trade_mark or trade.get("last_mark") or trade["lead_entry_price"])
            reason = "lead_position_disappeared_mark_fallback"
            trade["last_history_match_reject"] = hist_meta
        intents.append(
            {
                "intent_type": "close_position",
                "action": "CLOSE",
                "key": key,
                "trade": trade,
                "expected_exit": exit_price,
                "reason": reason,
                "history_row": hist,
                "history_match": hist_meta,
                "strategy_policy": "source_close_closes_follower",
            }
        )

    return {"current_keys": set(current), "events": events, "intents": intents}
