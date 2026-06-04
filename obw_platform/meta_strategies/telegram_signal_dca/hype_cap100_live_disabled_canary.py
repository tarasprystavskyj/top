#!/usr/bin/env python3
"""Live-disabled HYPE cap100 canary wrapper.

Paper-only by construction: this module reads public copy-trading/market data
or explicit mock inputs, writes local paper state, and contains no exchange
order submission path.
"""
import argparse
import copy
import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from paper_live_binance_copy_public_positions import (  # noqa: E402
    annotate_positions_with_lead_margin_metadata,
    extract_lead_margin_balance_by_label,
    fetch_open_positions,
    fetch_lead_margin_balance,
    fetch_position_history,
    iso,
    parse_float,
    parse_lead_margin_balance_usdt,
    ret_for,
    source_margin_allocation_metadata,
    utc_now,
)
try:
    from . import hype_cap100_champion_dca_strategy as champion_dca  # noqa: E402
    from . import hype_copy_signal_meta_strategy as copy_signal_meta  # noqa: E402
except ImportError:  # pragma: no cover - script import path
    import hype_cap100_champion_dca_strategy as champion_dca  # noqa: E402
    import hype_copy_signal_meta_strategy as copy_signal_meta  # noqa: E402


DEFAULT_PORTFOLIO_ID = "4300516091842181632"
DEFAULT_SYMBOL = "HYPEUSDT"
DEFAULT_OUT_DIR = Path("reports/hype_canary_live_disabled_20260525")
DEFAULT_DEADLINE_UTC = ""

CHAMPION_CANDIDATE_INDEX = champion_dca.CHAMPION_CANDIDATE_INDEX
CHAMPION_PARAMS = champion_dca.CHAMPION_PARAMS
DCA_DROPS_PCT = champion_dca.DCA_DROPS_PCT
DCA_MULTIPLIERS = champion_dca.DCA_MULTIPLIERS
FEE_RATE = 0.0005
SLIPPAGE_BP = 4.25
SLIPPAGE_RATE = SLIPPAGE_BP / 10_000.0

BOOK_TICKER_URL = "https://fapi.binance.com/fapi/v1/ticker/bookTicker"
PREMIUM_INDEX_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"


def parse_utc(raw: str) -> datetime:
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except AttributeError:
        sign_pos = max(text.rfind("+"), text.rfind("-", 10))
        offset = None
        main = text
        if sign_pos > 10:
            main = text[:sign_pos]
            off = text[sign_pos:]
            sign = 1 if off[0] == "+" else -1
            hh, mm = off[1:].split(":", 1)
            offset = timezone(sign * timedelta(hours=int(hh), minutes=int(mm)))
        try:
            dt = datetime.strptime(main, "%Y-%m-%dT%H:%M:%S.%f")
        except ValueError:
            dt = datetime.strptime(main, "%Y-%m-%dT%H:%M:%S")
        if offset is not None:
            dt = dt.replace(tzinfo=offset)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_optional_deadline_utc(raw: str) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text or text.lower() in {"0", "none", "never", "disabled"}:
        return None
    return parse_utc(text)


def deadline_reached(now: datetime, deadline_utc: str) -> bool:
    deadline = parse_optional_deadline_utc(deadline_utc)
    return bool(deadline and now >= deadline)


def load_json(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    if not path.exists():
        return copy.deepcopy(default)
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def append_jsonl(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def paper_fill_price(side: str, expected_price: float, action: str) -> float:
    if side == "LONG":
        return expected_price * (1.0 + SLIPPAGE_RATE) if action == "entry" else expected_price * (1.0 - SLIPPAGE_RATE)
    return expected_price * (1.0 - SLIPPAGE_RATE) if action == "entry" else expected_price * (1.0 + SLIPPAGE_RATE)


def default_state(args: argparse.Namespace) -> Dict[str, Any]:
    return {
        "mode": "hype_cap100_live_disabled_canary",
        "paper_only": True,
        "candidate_index": CHAMPION_CANDIDATE_INDEX,
        "candidate_params": CHAMPION_PARAMS,
        "portfolio_id": args.portfolio_id,
        "symbol": args.symbol,
        "initial_equity": args.initial_equity,
        "equity": args.initial_equity,
        "open_trades": {},
        "closed_trades": [],
        "paper_orders": [],
        "events": [],
        "last_poll": None,
    }


def open_notional_by_side(open_trades: Dict[str, Dict[str, Any]]) -> Dict[str, float]:
    out: Dict[str, float] = {"LONG": 0.0, "SHORT": 0.0}
    for trade in open_trades.values():
        side = str(trade.get("side") or "").upper()
        if side in out:
            out[side] += float(trade.get("notional") or 0.0)
    return out


def unrealized_pnl(trade: Dict[str, Any], mark: Optional[float]) -> float:
    if not mark or mark <= 0:
        return 0.0
    gross = ret_for(str(trade["side"]), float(trade["avg_entry"]), mark) * float(trade["notional"])
    exit_fee = float(trade["notional"]) * FEE_RATE
    return gross - float(trade.get("fees_paid") or 0.0) - exit_fee


def daily_pnl_usdt(state: Dict[str, Any], mark: Optional[float], now: datetime) -> float:
    day_start_ms = int(datetime(now.year, now.month, now.day, tzinfo=timezone.utc).timestamp() * 1000)
    realized = 0.0
    for trade in state.get("closed_trades", []):
        if int(trade.get("closed_at_ms") or 0) >= day_start_ms:
            realized += float(trade.get("paper_pnl_usdt") or 0.0)
    unrealized = sum(unrealized_pnl(t, mark) for t in state.get("open_trades", {}).values())
    return realized + unrealized


def recent_new_orders(state: Dict[str, Any], now: datetime) -> int:
    cutoff = int(now.timestamp() * 1000) - 3_600_000
    return sum(1 for row in state.get("paper_orders", []) if int(row.get("ts_ms") or 0) >= cutoff and row.get("risk_action") == "new_entry")


def dca_levels(entry_price: float) -> List[float]:
    return champion_dca.dca_levels(entry_price)


def build_plan(equity: float, args: argparse.Namespace, entry_price: float) -> Dict[str, Any]:
    return champion_dca.build_plan(equity, args, entry_price)


def guard_new_entry(
    state: Dict[str, Any],
    *,
    side: str,
    add_notional: float,
    mark: Optional[float],
    now: datetime,
    args: argparse.Namespace,
) -> Tuple[bool, str, Dict[str, Any]]:
    side_notionals = open_notional_by_side(state.get("open_trades", {}))
    gross = sum(side_notionals.values())
    daily_pnl = daily_pnl_usdt(state, mark, now)
    hourly_orders = recent_new_orders(state, now)
    projected_side = side_notionals.get(side, 0.0) + add_notional
    projected_gross = gross + add_notional
    detail = {
        "daily_pnl_usdt": daily_pnl,
        "hourly_new_orders": hourly_orders,
        "gross_open_notional": gross,
        "one_side_open_notional": side_notionals.get(side, 0.0),
        "projected_gross_open_notional": projected_gross,
        "projected_one_side_open_notional": projected_side,
    }
    if deadline_reached(now, args.deadline_utc):
        return False, "runtime_deadline_reached", detail
    if daily_pnl <= -abs(float(args.max_daily_loss_usdt)):
        return False, "daily_loss_guard", detail
    if hourly_orders >= int(args.max_orders_per_hour):
        return False, "max_orders_per_hour_guard", detail
    if projected_gross > float(args.max_gross_notional_usdt) + 1e-9:
        return False, "gross_notional_guard", detail
    if projected_side > float(args.max_one_side_notional_usdt) + 1e-9:
        return False, "one_side_notional_guard", detail
    return True, "allowed", detail


def add_fill(
    state: Dict[str, Any],
    trade: Dict[str, Any],
    *,
    now: datetime,
    expected_price: float,
    notional: float,
    fill_type: str,
    reason: str,
    mark: Optional[float],
    args: argparse.Namespace,
) -> Dict[str, Any]:
    ok, guard_reason, guard_detail = guard_new_entry(state, side=str(trade["side"]), add_notional=notional, mark=mark, now=now, args=args)
    if not ok:
        return {
            "type": "paper_entry_blocked",
            "key": trade.get("key"),
            "fill_type": fill_type,
            "reason": guard_reason,
            "guard": guard_detail,
            "requested_notional": notional,
        }
    paper_price = paper_fill_price(str(trade["side"]), expected_price, "entry")
    qty = notional / max(paper_price, 1e-12)
    trade["qty"] = float(trade.get("qty") or 0.0) + qty
    trade["notional"] = float(trade.get("notional") or 0.0) + notional
    trade["fees_paid"] = float(trade.get("fees_paid") or 0.0) + notional * FEE_RATE
    trade["avg_entry"] = trade["notional"] / max(trade["qty"], 1e-12)
    order = {
        "ts_ms": int(now.timestamp() * 1000),
        "utc": iso(now),
        "risk_action": "new_entry",
        "symbol": trade["symbol"],
        "side": trade["side"],
        "fill_type": fill_type,
        "reason": reason,
        "expected_price": expected_price,
        "paper_fill_price": paper_price,
        "notional": notional,
        "qty": qty,
        "lead_margin_balance_usdt": trade.get("lead_margin_balance_usdt"),
        "source_position_margin_usdt": trade.get("source_position_margin_usdt"),
        "source_position_margin_source": trade.get("source_position_margin_source"),
        "source_margin_fraction": trade.get("source_margin_fraction"),
        "source_margin_fraction_reason": trade.get("source_margin_fraction_reason"),
        "paper_only": True,
    }
    state.setdefault("paper_orders", []).append(order)
    state["paper_orders"] = state["paper_orders"][-5000:]
    trade.setdefault("fills", []).append(order)
    return {"type": "paper_fill", "key": trade.get("key"), "fill": order, "guard": guard_detail}


def close_trade(trade: Dict[str, Any], *, now: datetime, expected_exit: float, mark: Optional[float], reason: str, history_row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    paper_exit = paper_fill_price(str(trade["side"]), expected_exit, "exit")
    gross = ret_for(str(trade["side"]), float(trade["avg_entry"]), paper_exit) * float(trade["notional"])
    exit_fee = float(trade["notional"]) * FEE_RATE
    pnl = gross - float(trade.get("fees_paid") or 0.0) - exit_fee
    closed = copy.deepcopy(trade)
    closed.update(
        {
            "closed_at_utc": iso(now),
            "closed_at_ms": int(now.timestamp() * 1000),
            "expected_exit_price": expected_exit,
            "paper_exit_price": paper_exit,
            "paper_pnl_usdt": pnl,
            "exit_fee": exit_fee,
            "exit_reason": reason,
            "history_exit": history_row,
            "last_mark": mark,
        }
    )
    return closed


def fetch_json(session: requests.Session, url: str, params: Dict[str, Any], timeout_sec: float) -> Optional[Dict[str, Any]]:
    try:
        resp = session.get(url, params=params, timeout=timeout_sec)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def fetch_mark(session: requests.Session, symbol: str, timeout_sec: float) -> Tuple[Optional[float], Dict[str, Any]]:
    premium = fetch_json(session, PREMIUM_INDEX_URL, {"symbol": symbol}, timeout_sec)
    book = fetch_json(session, BOOK_TICKER_URL, {"symbol": symbol}, timeout_sec)
    mark = parse_float((premium or {}).get("markPrice"))
    bid = parse_float((book or {}).get("bidPrice"))
    ask = parse_float((book or {}).get("askPrice"))
    mid = (bid + ask) / 2.0 if math.isfinite(bid) and math.isfinite(ask) and bid > 0 and ask > 0 else math.nan
    out = mark if math.isfinite(mark) and mark > 0 else mid
    return (out if math.isfinite(out) and out > 0 else None), {"premium_ok": bool(premium), "book_ok": bool(book), "mark": out if math.isfinite(out) else None}


def mock_positions(args: argparse.Namespace) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[float], Dict[str, Any]]:
    side = "SHORT" if args.mock_open_short else "LONG"
    if args.mock_no_position:
        return [], [], args.mock_mark, {"mock": True, "mode": "no_position"}
    pos = {
        "key": f"{args.symbol}:{side}",
        "id": "mock_position_1",
        "symbol": args.symbol,
        "side": side,
        "entry_price": args.mock_entry,
        "mark_price": args.mock_mark,
        "position_amount": 1.0,
        "notional_value": args.mock_entry,
        "leverage": 1,
        "unrealized_profit": 0.0,
        "raw": {"mock": True},
    }
    return [pos], [], args.mock_mark, {"mock": True, "mode": "open_position", "side": side}


def load_inputs(args: argparse.Namespace) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[float], Dict[str, Any]]:
    if args.mock_open_long or args.mock_open_short or args.mock_no_position:
        return mock_positions(args)
    session = requests.Session()
    positions, positions_meta = fetch_open_positions(session, args.portfolio_id, args.timeout_sec)
    history, history_meta = fetch_position_history(session, args.portfolio_id, args.timeout_sec, page_size=args.history_page_size)
    mark, mark_meta = fetch_mark(session, args.symbol, args.timeout_sec)
    lead_page_meta = fetch_lead_margin_balance(session, args.portfolio_id, args.timeout_sec)
    annotate_positions_with_lead_margin_metadata(positions, lead_page_meta)
    return positions, history, mark, {"positions": positions_meta, "history": history_meta, "market": mark_meta, "lead_page": lead_page_meta}


def apply_snapshot(state: Dict[str, Any], positions: List[Dict[str, Any]], history: List[Dict[str, Any]], mark: Optional[float], now: datetime, args: argparse.Namespace) -> List[Dict[str, Any]]:
    plan = copy_signal_meta.build_strategy_intents(state, positions, history, mark, now, args, allow_dca=True, iso_fn=iso)
    events: List[Dict[str, Any]] = list(plan.get("events") or [])
    open_trades: Dict[str, Dict[str, Any]] = state.setdefault("open_trades", {})
    dca_blocked_keys = set()

    for intent in plan.get("intents") or []:
        key = str(intent.get("key") or "")
        if intent.get("action") == "OPEN":
            if intent.get("intent_type") == "dca_entry" and key in dca_blocked_keys:
                continue
            trade = intent["trade"]
            if intent.get("intent_type") == "dca_entry" and key not in open_trades:
                events.append({"type": "paper_entry_blocked", "key": key, "fill_type": intent.get("fill_type"), "reason": "strategy_intent_without_open_trade"})
                dca_blocked_keys.add(key)
                continue
            event = add_fill(
                state,
                trade,
                now=now,
                expected_price=float(intent["expected_price"]),
                notional=float(intent["notional"]),
                fill_type=str(intent["fill_type"]),
                reason=str(intent["reason"]),
                mark=mark,
                args=args,
            )
            events.append(event)
            if event["type"] == "paper_fill" and intent.get("intent_type") == "open_entry":
                open_trades[key] = trade
            if event["type"] == "paper_fill" and intent.get("intent_type") == "dca_entry":
                trade["next_level_idx"] = int(intent.get("level_idx") or 0) + 1
            elif event["type"] != "paper_fill":
                dca_blocked_keys.add(key)
            continue
        if intent.get("action") != "CLOSE":
            events.append({"type": "strategy_intent_ignored", "key": key, "reason": "unsupported_intent", "intent": intent})
            continue
        trade = intent["trade"]
        closed = close_trade(trade, now=now, expected_exit=float(intent["expected_exit"]), mark=mark, reason=str(intent["reason"]), history_row=intent.get("history_row"))
        state["equity"] = float(state.get("equity") or args.initial_equity) + float(closed["paper_pnl_usdt"])
        state.setdefault("closed_trades", []).append(closed)
        if key in open_trades:
            del open_trades[key]
        events.append({"type": "paper_exit", "key": key, "pnl": closed["paper_pnl_usdt"], "reason": intent.get("reason"), "strategy_policy": intent.get("strategy_policy")})

    return events


def status_payload(state: Dict[str, Any], mark: Optional[float], now: datetime, events: List[Dict[str, Any]], input_meta: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    side_notionals = open_notional_by_side(state.get("open_trades", {}))
    open_summary = []
    for trade in state.get("open_trades", {}).values():
        upnl = unrealized_pnl(trade, mark)
        open_summary.append(
            {
                "key": trade["key"],
                "symbol": trade["symbol"],
                "side": trade["side"],
                "notional": trade["notional"],
                "avg_entry": trade["avg_entry"],
                "last_mark": mark,
                "unrealized_pnl_usdt": upnl,
                "fills": len(trade.get("fills") or []),
                "next_level_idx": trade.get("next_level_idx"),
                "lead_margin_balance_usdt": trade.get("lead_margin_balance_usdt"),
                "source_position_margin_usdt": trade.get("source_position_margin_usdt"),
                "source_position_margin_source": trade.get("source_position_margin_source"),
                "source_margin_fraction": trade.get("source_margin_fraction"),
                "source_margin_fraction_reason": trade.get("source_margin_fraction_reason"),
            }
        )
    return {
        "utc": iso(now),
        "paper_only": True,
        "live_order_code_present": False,
        "candidate_index": CHAMPION_CANDIDATE_INDEX,
        "candidate_params": CHAMPION_PARAMS,
        "portfolio_id": args.portfolio_id,
        "symbol": args.symbol,
        "long_only": bool(args.long_only),
        "deadline_utc": args.deadline_utc,
        "guards": {
            "max_gross_notional_usdt": args.max_gross_notional_usdt,
            "max_one_side_notional_usdt": args.max_one_side_notional_usdt,
            "max_daily_loss_usdt": args.max_daily_loss_usdt,
            "max_orders_per_hour": args.max_orders_per_hour,
            "daily_realized_plus_unrealized_pnl_usdt": daily_pnl_usdt(state, mark, now),
            "hourly_new_orders": recent_new_orders(state, now),
            "gross_open_notional": sum(side_notionals.values()),
            "one_side_open_notional": side_notionals,
        },
        "state_path": str(args.state_path),
        "telemetry_path": str(args.telemetry_path),
        "open_paper_trades": open_summary,
        "closed_paper_trades": len(state.get("closed_trades", [])),
        "events": events,
        "input_meta": input_meta,
    }


def poll_once(args: argparse.Namespace) -> Dict[str, Any]:
    now = utc_now()
    state = load_json(Path(args.state_path), default_state(args))
    positions, history, mark, input_meta = load_inputs(args)
    events = apply_snapshot(state, positions, history, mark, now, args)
    status = status_payload(state, mark, now, events, input_meta, args)
    state["last_poll"] = status
    state.setdefault("events", []).extend({"utc": iso(now), **event} for event in events)
    state["events"] = state["events"][-args.max_events :]
    write_json(Path(args.state_path), state)
    write_json(Path(args.status_path), status)
    append_jsonl(Path(args.telemetry_path), {"event": "poll", "status": status})
    for event in events:
        append_jsonl(Path(args.telemetry_path), {"event": "canary_event", "utc": iso(now), **event})
    return status


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Paper-only HYPE cap100 live-disabled canary.")
    ap.add_argument("--portfolio-id", default=DEFAULT_PORTFOLIO_ID)
    ap.add_argument("--symbol", default=DEFAULT_SYMBOL)
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    ap.add_argument("--state-path", default="")
    ap.add_argument("--status-path", default="")
    ap.add_argument("--telemetry-path", default="")
    ap.add_argument("--initial-equity", type=float, default=30.0)
    ap.add_argument("--initial-target-notional", type=float, default=30.0)
    ap.add_argument("--max-gross-notional-usdt", type=float, default=30.0)
    ap.add_argument("--max-one-side-notional-usdt", type=float, default=30.0)
    ap.add_argument("--max-daily-loss-usdt", type=float, default=5.0)
    ap.add_argument("--max-orders-per-hour", type=int, default=20)
    ap.add_argument("--deadline-utc", default=DEFAULT_DEADLINE_UTC, help="Optional runtime stop time. Empty/none/never/disabled means run indefinitely.")
    ap.add_argument("--long-only", action="store_true", default=True)
    ap.add_argument("--history-page-size", type=int, default=50)
    ap.add_argument("--timeout-sec", type=float, default=20.0)
    ap.add_argument("--interval-sec", type=float, default=60.0)
    ap.add_argument("--max-events", type=int, default=2000)
    ap.add_argument("--mock-open-long", action="store_true")
    ap.add_argument("--mock-open-short", action="store_true")
    ap.add_argument("--mock-no-position", action="store_true")
    ap.add_argument("--mock-entry", type=float, default=50.0)
    ap.add_argument("--mock-mark", type=float, default=50.0)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--loop", action="store_true")
    mode.add_argument("--once", action="store_true")
    return ap


def normalize_paths(args: argparse.Namespace) -> None:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not args.state_path:
        args.state_path = str(out_dir / "state.json")
    if not args.status_path:
        args.status_path = str(out_dir / "STATUS.json")
    if not args.telemetry_path:
        args.telemetry_path = str(out_dir / "telemetry.jsonl")


def validate_args(args: argparse.Namespace) -> None:
    if args.initial_equity <= 0 or args.initial_target_notional <= 0:
        raise SystemExit("initial equity and target notional must be positive")
    if args.max_gross_notional_usdt <= 0 or args.max_one_side_notional_usdt <= 0:
        raise SystemExit("notional guards must be positive")
    if args.max_orders_per_hour <= 0:
        raise SystemExit("--max-orders-per-hour must be positive")
    if args.interval_sec <= 0:
        raise SystemExit("--interval-sec must be positive")
    parse_optional_deadline_utc(args.deadline_utc)


def main() -> None:
    args = build_arg_parser().parse_args()
    normalize_paths(args)
    validate_args(args)
    while True:
        print(json.dumps(poll_once(args), ensure_ascii=False, indent=2, sort_keys=True), flush=True)
        if not args.loop:
            break
        if deadline_reached(utc_now(), args.deadline_utc):
            break
        time.sleep(args.interval_sec)


if __name__ == "__main__":
    main()
