#!/usr/bin/env python3
"""Paper-live tracker for public Binance copy-trading lead positions.

This is read-only. It polls Binance public/friendly frontend endpoints for one
copy-trading lead portfolio and keeps a local paper state. It never places
orders and defaults to a single poll.
"""
import argparse
import copy
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests


DEFAULT_PORTFOLIO_ID = "4728671486012660992"
DEFAULT_REPORT_DIR = (
    "obw_platform/meta_strategies/telegram_signal_dca/"
    "reports/binance_copy_4728671486012660992_20260519"
)
POSITIONS_URL = "https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade/lead-data/positions"
POSITION_HISTORY_URL = "https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/position-history"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def ms_to_iso(raw: Any) -> Optional[str]:
    try:
        val = int(raw)
    except Exception:
        return None
    if val <= 0:
        return None
    return iso(datetime.fromtimestamp(val / 1000.0, tz=timezone.utc))


def parse_float(raw: Any, default: float = math.nan) -> float:
    try:
        if raw in ("", None):
            return default
        return float(str(raw).replace(",", ""))
    except Exception:
        return default


def finite_pos(raw: Any) -> Optional[float]:
    val = parse_float(raw)
    if math.isfinite(val) and val > 0:
        return val
    return None


def normalize_side(raw: Any, amount: float = 0.0) -> str:
    side = str(raw or "").upper().strip()
    if side in {"LONG", "SHORT"}:
        return side
    if side == "BOTH":
        return "LONG" if amount > 0 else "SHORT"
    if side in {"BUY", "Long".upper()}:
        return "LONG"
    if side in {"SELL", "Short".upper()}:
        return "SHORT"
    return "UNKNOWN"


def ret_for(side: str, entry: float, exit_px: float) -> float:
    if entry <= 0 or exit_px <= 0:
        return 0.0
    if side == "LONG":
        return exit_px / entry - 1.0
    if side == "SHORT":
        return entry / exit_px - 1.0
    return 0.0


def request_json(
    session: requests.Session,
    method: str,
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    payload: Optional[Dict[str, Any]] = None,
    timeout_sec: float,
) -> Dict[str, Any]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "Origin": "https://www.binance.com",
        "Referer": f"https://www.binance.com/en/copy-trading/lead-details/{DEFAULT_PORTFOLIO_ID}",
    }
    if method == "GET":
        resp = session.get(url, params=params, headers=headers, timeout=timeout_sec)
    else:
        resp = session.post(url, json=payload, headers=headers, timeout=timeout_sec)
    resp.raise_for_status()
    data = resp.json()
    if str(data.get("code")) != "000000":
        raise RuntimeError(f"Binance response code={data.get('code')} message={data.get('message')}")
    return data


def fetch_open_positions(session: requests.Session, portfolio_id: str, timeout_sec: float) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    raw = request_json(
        session,
        "GET",
        POSITIONS_URL,
        params={"portfolioId": portfolio_id},
        timeout_sec=timeout_sec,
    )
    data = raw.get("data") or []
    if not isinstance(data, list):
        raise RuntimeError(f"unexpected positions payload data type: {type(data).__name__}")
    positions: List[Dict[str, Any]] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        amount = parse_float(row.get("positionAmount"), 0.0)
        entry = finite_pos(row.get("entryPrice")) or finite_pos(row.get("breakEvenPrice"))
        notional = abs(parse_float(row.get("notionalValue"), 0.0))
        if abs(amount) <= 0 and notional <= 0:
            continue
        symbol = str(row.get("symbol") or "").upper().strip()
        side = normalize_side(row.get("positionSide"), amount)
        if not symbol or side not in {"LONG", "SHORT"} or not entry:
            continue
        mark = finite_pos(row.get("markPrice"))
        position_id = str(row.get("id") or f"{symbol}_{side}")
        positions.append(
            {
                "key": f"{symbol}:{side}",
                "id": position_id,
                "symbol": symbol,
                "side": side,
                "entry_price": entry,
                "mark_price": mark,
                "position_amount": amount,
                "notional_value": parse_float(row.get("notionalValue"), 0.0),
                "leverage": row.get("leverage"),
                "unrealized_profit": parse_float(row.get("unrealizedProfit"), 0.0),
                "break_even_price": finite_pos(row.get("breakEvenPrice")),
                "raw": row,
            }
        )
    meta = {"endpoint": POSITIONS_URL, "raw_rows": len(data), "open_rows": len(positions)}
    return positions, meta


def fetch_position_history(
    session: requests.Session,
    portfolio_id: str,
    timeout_sec: float,
    *,
    page_size: int,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    payload = {
        "portfolioId": portfolio_id,
        "pageNumber": 1,
        "pageSize": page_size,
        "timeRange": "365D",
    }
    raw = request_json(session, "POST", POSITION_HISTORY_URL, payload=payload, timeout_sec=timeout_sec)
    data = raw.get("data") or {}
    rows = data.get("list") or []
    if not isinstance(rows, list):
        raise RuntimeError(f"unexpected position-history list type: {type(rows).__name__}")
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper().strip()
        side = normalize_side(row.get("side"))
        if not symbol or side not in {"LONG", "SHORT"}:
            continue
        out.append(
            {
                "id": str(row.get("id") or ""),
                "key": f"{symbol}:{side}",
                "symbol": symbol,
                "side": side,
                "opened_ms": row.get("opened"),
                "opened_utc": ms_to_iso(row.get("opened")),
                "closed_ms": row.get("closed"),
                "closed_utc": ms_to_iso(row.get("closed")),
                "avg_cost": finite_pos(row.get("avgCost")),
                "avg_close_price": finite_pos(row.get("avgClosePrice")),
                "closing_pnl": parse_float(row.get("closingPnl"), 0.0),
                "status": row.get("status"),
                "leverage": row.get("leverage"),
                "roi": parse_float(row.get("roi"), 0.0),
                "raw": row,
            }
        )
    meta = {
        "endpoint": POSITION_HISTORY_URL,
        "request": payload,
        "total": data.get("total"),
        "rows": len(out),
    }
    return out, meta


def default_state(portfolio_id: str) -> Dict[str, Any]:
    return {
        "portfolio_id": portfolio_id,
        "paper_notional_usdt": 100.0,
        "open_positions": {},
        "closed_trades": [],
        "events": [],
        "last_poll": None,
    }


def load_state(path: Path, portfolio_id: str, notional: float) -> Dict[str, Any]:
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
    else:
        state = default_state(portfolio_id)
    state.setdefault("portfolio_id", portfolio_id)
    state["paper_notional_usdt"] = notional
    state.setdefault("open_positions", {})
    state.setdefault("closed_trades", [])
    state.setdefault("events", [])
    return state


def find_history_exit(open_trade: Dict[str, Any], history: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    key = open_trade.get("key")
    detected_ms = open_trade.get("detected_at_ms") or 0
    candidates: List[Dict[str, Any]] = []
    for row in history:
        if row.get("key") != key or not row.get("avg_close_price"):
            continue
        closed_ms = int(row.get("closed_ms") or 0)
        opened_ms = int(row.get("opened_ms") or 0)
        if closed_ms and closed_ms >= int(detected_ms):
            candidates.append(row)
        elif opened_ms and detected_ms and abs(opened_ms - int(detected_ms)) <= 86_400_000:
            candidates.append(row)
    if not candidates:
        return None
    return sorted(candidates, key=lambda r: int(r.get("closed_ms") or 0), reverse=True)[0]


def close_trade(
    open_trade: Dict[str, Any],
    *,
    exit_price: float,
    exit_reason: str,
    now: datetime,
    history_row: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    entry = float(open_trade["entry_price"])
    side = str(open_trade["side"])
    notional = float(open_trade["paper_notional_usdt"])
    pnl = ret_for(side, entry, exit_price) * notional
    closed = copy.deepcopy(open_trade)
    closed.update(
        {
            "exit_price": exit_price,
            "exit_reason": exit_reason,
            "exited_at_utc": iso(now),
            "exited_at_ms": int(now.timestamp() * 1000),
            "paper_pnl_usdt": pnl,
            "paper_return_pct": 100.0 * pnl / max(notional, 1e-12),
            "history_exit": history_row,
        }
    )
    return closed


def apply_snapshot(
    state: Dict[str, Any],
    open_snapshot: List[Dict[str, Any]],
    history: List[Dict[str, Any]],
    *,
    now: datetime,
    notional: float,
) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    current = {pos["key"]: pos for pos in open_snapshot}
    state_open: Dict[str, Dict[str, Any]] = state["open_positions"]

    for key, pos in sorted(current.items()):
        if key in state_open:
            state_open[key]["last_seen_utc"] = iso(now)
            state_open[key]["last_mark_price"] = pos.get("mark_price")
            state_open[key]["last_raw_position"] = pos.get("raw")
            continue
        trade = {
            "key": key,
            "lead_position_id": pos.get("id"),
            "symbol": pos["symbol"],
            "side": pos["side"],
            "detected_at_utc": iso(now),
            "detected_at_ms": int(now.timestamp() * 1000),
            "entry_price": pos["entry_price"],
            "entry_price_source": "positions.entryPrice",
            "paper_notional_usdt": notional,
            "lead_position_amount": pos.get("position_amount"),
            "lead_notional_value": pos.get("notional_value"),
            "lead_leverage": pos.get("leverage"),
            "last_mark_price": pos.get("mark_price"),
            "last_seen_utc": iso(now),
            "raw_entry_position": pos.get("raw"),
        }
        state_open[key] = trade
        events.append({"type": "paper_entry", "key": key, "symbol": pos["symbol"], "side": pos["side"], "price": pos["entry_price"]})

    keys_to_close = set(state_open) - set(current)
    for key in set(state_open) & set(current):
        hist = find_history_exit(state_open[key], history)
        if hist and hist.get("avg_close_price"):
            keys_to_close.add(key)

    for key in sorted(keys_to_close):
        open_trade = state_open[key]
        hist = find_history_exit(open_trade, history)
        if hist and hist.get("avg_close_price"):
            exit_price = float(hist["avg_close_price"])
            exit_reason = "position_history_closed"
        else:
            exit_price = float(open_trade.get("last_mark_price") or open_trade["entry_price"])
            exit_reason = "disappeared_no_history_price_fallback"
        closed = close_trade(open_trade, exit_price=exit_price, exit_reason=exit_reason, now=now, history_row=hist)
        state["closed_trades"].append(closed)
        del state_open[key]
        events.append(
            {
                "type": "paper_exit",
                "key": key,
                "symbol": closed["symbol"],
                "side": closed["side"],
                "price": exit_price,
                "pnl": closed["paper_pnl_usdt"],
                "reason": exit_reason,
            }
        )

    return events


def endpoint_fields(open_positions: List[Dict[str, Any]], history: List[Dict[str, Any]]) -> Dict[str, Any]:
    open_raw_keys = sorted({k for p in open_positions for k in (p.get("raw") or {}).keys()})
    hist_raw_keys = sorted({k for h in history for k in (h.get("raw") or {}).keys()})
    return {
        "positions_found_fields": open_raw_keys,
        "position_history_found_fields": hist_raw_keys,
        "positions_missing_fields_needed_for_exact_lead_open_time": ["opened", "openTime", "updateTime"],
        "entry_price_fields_used": ["entryPrice", "breakEvenPrice fallback"],
        "exit_price_fields_used": ["position-history.avgClosePrice", "last markPrice fallback"],
    }


def poll_once(args: argparse.Namespace) -> Dict[str, Any]:
    now = utc_now()
    state_path = Path(args.state_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    open_positions, positions_meta = fetch_open_positions(session, args.portfolio_id, args.timeout_sec)
    history, history_meta = fetch_position_history(
        session,
        args.portfolio_id,
        args.timeout_sec,
        page_size=args.history_page_size,
    )
    state = load_state(state_path, args.portfolio_id, args.notional_usdt)
    before = copy.deepcopy(state)
    events = apply_snapshot(state, open_positions, history, now=now, notional=args.notional_usdt)
    state["last_poll"] = {
        "utc": iso(now),
        "positions": positions_meta,
        "position_history": history_meta,
        "events": events,
        "endpoint_fields": endpoint_fields(open_positions, history),
    }
    state["events"].extend({"utc": iso(now), **event} for event in events)
    if len(state["events"]) > args.max_events:
        state["events"] = state["events"][-args.max_events :]
    if not args.dry_run:
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "dry_run": args.dry_run,
        "state_path": str(state_path),
        "open_positions_seen": len(open_positions),
        "history_rows_seen": len(history),
        "events": events,
        "open_paper_positions_before": len(before.get("open_positions", {})),
        "open_paper_positions_after": len(state.get("open_positions", {})),
        "closed_paper_trades_after": len(state.get("closed_trades", [])),
        "endpoint_fields": state["last_poll"]["endpoint_fields"],
    }


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Paper-live Binance copy-trading public-position tracker.")
    ap.add_argument("--portfolio-id", default=DEFAULT_PORTFOLIO_ID)
    ap.add_argument("--state-path", default=f"{DEFAULT_REPORT_DIR}/paper_live_state.json")
    ap.add_argument("--notional-usdt", type=float, default=100.0)
    ap.add_argument("--history-page-size", type=int, default=20)
    ap.add_argument("--timeout-sec", type=float, default=20.0)
    ap.add_argument("--dry-run", action="store_true", help="Fetch and compute actions without writing state.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--loop", action="store_true", help="Poll repeatedly. Without this flag the script runs once.")
    mode.add_argument("--once", action="store_true", help="Explicit single-poll mode; this is the default.")
    ap.add_argument("--interval-sec", type=float, default=60.0)
    ap.add_argument("--max-events", type=int, default=1000)
    return ap


def main() -> None:
    args = build_arg_parser().parse_args()
    if args.interval_sec <= 0:
        raise SystemExit("--interval-sec must be positive")
    if args.notional_usdt <= 0:
        raise SystemExit("--notional-usdt must be positive")

    while True:
        result = poll_once(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not args.loop:
            break
        time.sleep(args.interval_sec)


if __name__ == "__main__":
    main()
