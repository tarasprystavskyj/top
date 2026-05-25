#!/usr/bin/env python3
"""Paper-live runner for the grounded HYPE compound DCA champion.

Read-only/paper-only. The Binance copy-trading public open-position endpoint is
the signal source. No exchange order endpoint or secret is used.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from paper_live_binance_copy_public_positions import (  # noqa: E402
    fetch_open_positions,
    fetch_position_history,
    find_history_exit,
    iso,
    ret_for,
    utc_now,
)


DEFAULT_PORTFOLIO_ID = "4300516091842181632"
DEFAULT_SYMBOL = "HYPEUSDT"
DEFAULT_REPORT_DIR = (
    Path("obw_platform")
    / "meta_strategies"
    / "telegram_signal_dca"
    / "reports"
    / "binance_430051_hype_v21_loop_20260523"
    / "paper_live_grounded_compound_champion"
)

CHAMPION_NAME = "t500_b16_s0p25-0p35-0p55_w0p8-1p2-2p2"
INITIAL_EQUITY = 500.0
INITIAL_TARGET_NOTIONAL = 500.0
BASE_FRAC = 0.16
STEPS_PCT = (0.25, 0.35, 0.55)
ADD_WEIGHTS = (0.8, 1.2, 2.2)
FEE_RATE = 0.0005
SLIPPAGE_BP = 4.25
SLIPPAGE_RATE = SLIPPAGE_BP / 10_000.0
MIN_ORDER_USD_FALLBACK = 2.0

BOOK_TICKER_URL = "https://fapi.binance.com/fapi/v1/ticker/bookTicker"
PREMIUM_INDEX_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"


@dataclass(frozen=True)
class FillPlan:
    target_notional: float
    base_notional: float
    add_notionals: Tuple[float, ...]
    levels: Tuple[float, ...]


def parse_float(raw: Any, default: float = math.nan) -> float:
    try:
        if raw in ("", None):
            return default
        return float(str(raw).replace(",", ""))
    except Exception:
        return default


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


def default_state(args: argparse.Namespace) -> Dict[str, Any]:
    return {
        "mode": "paper_live_grounded_compound_champion",
        "paper_only": True,
        "portfolio_id": args.portfolio_id,
        "symbol": args.symbol,
        "champion": CHAMPION_NAME,
        "equity": args.initial_equity,
        "initial_equity": args.initial_equity,
        "initial_target_notional": args.initial_target_notional,
        "open_trades": {},
        "closed_trades": [],
        "events": [],
        "last_poll": None,
    }


def fetch_json(session: requests.Session, url: str, params: Dict[str, Any], timeout_sec: float) -> Optional[Dict[str, Any]]:
    try:
        resp = session.get(url, params=params, timeout=timeout_sec)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def fetch_market_context(session: requests.Session, symbol: str, timeout_sec: float) -> Dict[str, Any]:
    before = time.time()
    bingx_price = math.nan
    bingx_market = None
    bingx_bid = math.nan
    bingx_ask = math.nan
    bingx_bid_size = math.nan
    bingx_ask_size = math.nan
    bingx_book_bids_top5: List[List[float]] = []
    bingx_book_asks_top5: List[List[float]] = []
    bingx_orderbook_error = None
    try:
        import ccxt  # type: ignore

        base = symbol[:-4] if symbol.endswith("USDT") else symbol
        bingx_market = f"{base}/USDT:USDT"
        ex = ccxt.bingx({"enableRateLimit": True, "timeout": int(timeout_sec * 1000)})
        ex.load_markets()
        if bingx_market in ex.markets:
            ticker = ex.fetch_ticker(bingx_market)
            bingx_price = parse_float(ticker.get("last"))
            if not math.isfinite(bingx_price):
                bingx_price = parse_float(ticker.get("mark"))
            bingx_bid = parse_float(ticker.get("bid"))
            bingx_ask = parse_float(ticker.get("ask"))
            try:
                orderbook = ex.fetch_order_book(bingx_market, limit=5) or {}
                bids = orderbook.get("bids") or []
                asks = orderbook.get("asks") or []
                if bids:
                    bingx_bid = parse_float(bids[0][0])
                    bingx_bid_size = parse_float(bids[0][1])
                if asks:
                    bingx_ask = parse_float(asks[0][0])
                    bingx_ask_size = parse_float(asks[0][1])
                for level in bids[:5]:
                    if len(level) >= 2:
                        px = parse_float(level[0])
                        sz = parse_float(level[1])
                        if math.isfinite(px) and math.isfinite(sz):
                            bingx_book_bids_top5.append([px, sz])
                for level in asks[:5]:
                    if len(level) >= 2:
                        px = parse_float(level[0])
                        sz = parse_float(level[1])
                        if math.isfinite(px) and math.isfinite(sz):
                            bingx_book_asks_top5.append([px, sz])
            except Exception as exc:
                bingx_orderbook_error = str(exc)[:200]
    except Exception:
        bingx_price = math.nan
    book = fetch_json(session, BOOK_TICKER_URL, {"symbol": symbol}, timeout_sec)
    after = time.time()
    premium = fetch_json(session, PREMIUM_INDEX_URL, {"symbol": symbol}, timeout_sec)
    bid = parse_float((book or {}).get("bidPrice"))
    ask = parse_float((book or {}).get("askPrice"))
    mark = parse_float((premium or {}).get("markPrice"))
    index = parse_float((premium or {}).get("indexPrice"))
    bingx_spread = bingx_ask - bingx_bid if math.isfinite(bingx_bid) and math.isfinite(bingx_ask) else math.nan
    bingx_mid = (bingx_ask + bingx_bid) / 2.0 if math.isfinite(bingx_bid) and math.isfinite(bingx_ask) else math.nan
    spread = ask - bid if math.isfinite(bid) and math.isfinite(ask) else math.nan
    mid = (ask + bid) / 2.0 if math.isfinite(bid) and math.isfinite(ask) else math.nan
    return {
        "source": "bingx_ccxt_ticker" if math.isfinite(bingx_price) and bingx_price > 0 else "missing_bingx_mark",
        "symbol": symbol,
        "bingx_market": bingx_market,
        "bingx_mark": bingx_price if math.isfinite(bingx_price) else None,
        "bingx_bid": bingx_bid if math.isfinite(bingx_bid) else None,
        "bingx_ask": bingx_ask if math.isfinite(bingx_ask) else None,
        "bingx_bid_size": bingx_bid_size if math.isfinite(bingx_bid_size) else None,
        "bingx_ask_size": bingx_ask_size if math.isfinite(bingx_ask_size) else None,
        "bingx_mid": bingx_mid if math.isfinite(bingx_mid) else None,
        "bingx_spread": bingx_spread if math.isfinite(bingx_spread) else None,
        "bingx_spread_bp_mid": (10_000.0 * bingx_spread / bingx_mid) if math.isfinite(bingx_spread) and math.isfinite(bingx_mid) and bingx_mid > 0 else None,
        "bingx_book_bids_top5": bingx_book_bids_top5,
        "bingx_book_asks_top5": bingx_book_asks_top5,
        "bingx_orderbook_ok": bool(bingx_book_bids_top5 and bingx_book_asks_top5),
        "bingx_orderbook_error": bingx_orderbook_error,
        "bid": bingx_bid if math.isfinite(bingx_bid) else None,
        "ask": bingx_ask if math.isfinite(bingx_ask) else None,
        "mid": bingx_mid if math.isfinite(bingx_mid) else None,
        "spread": bingx_spread if math.isfinite(bingx_spread) else None,
        "spread_bp_mid": (10_000.0 * bingx_spread / bingx_mid) if math.isfinite(bingx_spread) and math.isfinite(bingx_mid) and bingx_mid > 0 else None,
        "binance_bid_reference": bid if math.isfinite(bid) else None,
        "binance_ask_reference": ask if math.isfinite(ask) else None,
        "binance_mid_reference": mid if math.isfinite(mid) else None,
        "mark": bingx_price if math.isfinite(bingx_price) and bingx_price > 0 else None,
        "binance_mark_reference": mark if math.isfinite(mark) else None,
        "binance_index_reference": index if math.isfinite(index) else None,
        "index": index if math.isfinite(index) else None,
        "binance_spread_reference": spread if math.isfinite(spread) else None,
        "binance_spread_bp_mid_reference": (10_000.0 * spread / mid) if math.isfinite(spread) and math.isfinite(mid) and mid > 0 else None,
        "request_latency_ms": round((after - before) * 1000.0, 3),
        "raw_book_ok": bool(book),
        "raw_premium_ok": bool(premium),
    }


def fetch_symbol_rules(session: requests.Session, symbol: str, timeout_sec: float) -> Dict[str, Any]:
    data = fetch_json(session, EXCHANGE_INFO_URL, {"symbol": symbol}, timeout_sec)
    symbols = (data or {}).get("symbols") or []
    row = symbols[0] if symbols and isinstance(symbols[0], dict) else {}
    filters = row.get("filters") or []
    rules: Dict[str, Any] = {
        "min_notional": MIN_ORDER_USD_FALLBACK,
        "step_size": None,
        "tick_size": None,
        "raw_ok": bool(row),
    }
    for item in filters:
        if not isinstance(item, dict):
            continue
        ftype = item.get("filterType")
        if ftype in {"MIN_NOTIONAL", "NOTIONAL"}:
            rules["min_notional"] = parse_float(item.get("notional") or item.get("minNotional"), MIN_ORDER_USD_FALLBACK)
        elif ftype == "LOT_SIZE":
            rules["step_size"] = parse_float(item.get("stepSize"), math.nan)
        elif ftype == "PRICE_FILTER":
            rules["tick_size"] = parse_float(item.get("tickSize"), math.nan)
    for key in ("min_notional", "step_size", "tick_size"):
        if isinstance(rules.get(key), float) and not math.isfinite(rules[key]):
            rules[key] = None
    return rules


def paper_fill_price(side: str, expected_price: float, action: str, slippage_rate: float) -> float:
    if side == "LONG":
        if action == "entry":
            return expected_price * (1.0 + slippage_rate)
        return expected_price * (1.0 - slippage_rate)
    if action == "entry":
        return expected_price * (1.0 - slippage_rate)
    return expected_price * (1.0 + slippage_rate)


def dca_levels(side: str, entry: float) -> Tuple[float, ...]:
    levels: List[float] = []
    last = entry
    for step in STEPS_PCT:
        last = last * (1.0 - step / 100.0) if side == "LONG" else last * (1.0 + step / 100.0)
        levels.append(last)
    return tuple(levels)


def fill_plan(equity: float, args: argparse.Namespace, side: str, entry_price: float) -> FillPlan:
    target_frac = args.initial_target_notional / max(args.initial_equity, 1e-12)
    target = max(0.0, min(equity, equity * target_frac))
    base = target * BASE_FRAC
    remaining = max(target - base, 0.0)
    weight_sum = sum(ADD_WEIGHTS)
    adds = tuple(remaining * w / weight_sum for w in ADD_WEIGHTS)
    return FillPlan(target_notional=target, base_notional=base, add_notionals=adds, levels=dca_levels(side, entry_price))


def min_order_simulation(notional: float, price: float, rules: Dict[str, Any]) -> Dict[str, Any]:
    min_notional = parse_float(rules.get("min_notional"), MIN_ORDER_USD_FALLBACK)
    step_size = parse_float(rules.get("step_size"))
    qty = notional / max(price, 1e-12)
    rounded_qty = qty
    if math.isfinite(step_size) and step_size > 0:
        rounded_qty = math.floor(qty / step_size) * step_size
    rounded_notional = rounded_qty * price
    return {
        "notional": notional,
        "price": price,
        "qty_raw": qty,
        "qty_step_rounded": rounded_qty,
        "rounded_notional": rounded_notional,
        "min_notional": min_notional,
        "step_size": step_size if math.isfinite(step_size) else None,
        "would_reject_min_notional": rounded_notional < min_notional,
    }


def add_fill(
    trade: Dict[str, Any],
    *,
    now: datetime,
    expected_price: float,
    paper_price: float,
    notional: float,
    fill_type: str,
    fill_reason: str,
    equity_before: float,
    context: Dict[str, Any],
    rules: Dict[str, Any],
) -> Dict[str, Any]:
    side = str(trade["side"])
    qty = notional / max(paper_price, 1e-12)
    trade["qty"] = float(trade.get("qty", 0.0)) + qty
    trade["notional"] = float(trade.get("notional", 0.0)) + notional
    trade["fees_paid"] = float(trade.get("fees_paid", 0.0)) + notional * FEE_RATE
    trade["avg_entry"] = trade["notional"] / max(trade["qty"], 1e-12)
    fill = {
        "utc": iso(now),
        "type": fill_type,
        "side": side,
        "expected_price": expected_price,
        "paper_fill_price": paper_price,
        "slippage_bp": 10_000.0 * (paper_price / expected_price - 1.0) if expected_price > 0 else None,
        "notional": notional,
        "equity_before_fill": equity_before,
        "fee_rate": FEE_RATE,
        "fee": notional * FEE_RATE,
        "fill_reason": fill_reason,
        "candle_boundary": "live_mark_snapshot_no_candle_boundary",
        "market_context": context,
        "min_order_simulation": min_order_simulation(notional, paper_price, rules),
    }
    trade.setdefault("fills", []).append(fill)
    return fill


def unrealized_pnl(trade: Dict[str, Any], mark: Optional[float]) -> float:
    if not mark or mark <= 0:
        return 0.0
    side = str(trade["side"])
    gross = ret_for(side, float(trade["avg_entry"]), mark) * float(trade["notional"])
    exit_fee = float(trade["notional"]) * FEE_RATE
    return gross - float(trade.get("fees_paid", 0.0)) - exit_fee


def close_trade(
    trade: Dict[str, Any],
    *,
    now: datetime,
    expected_exit: float,
    exit_reason: str,
    history_row: Optional[Dict[str, Any]],
    context: Dict[str, Any],
) -> Dict[str, Any]:
    side = str(trade["side"])
    paper_exit = paper_fill_price(side, expected_exit, "exit", SLIPPAGE_RATE)
    gross_pnl = ret_for(side, float(trade["avg_entry"]), paper_exit) * float(trade["notional"])
    exit_fee = float(trade["notional"]) * FEE_RATE
    pnl = gross_pnl - float(trade.get("fees_paid", 0.0)) - exit_fee
    closed = copy.deepcopy(trade)
    closed.update(
        {
            "closed_at_utc": iso(now),
            "expected_exit_price": expected_exit,
            "paper_exit_price": paper_exit,
            "exit_slippage_bp": 10_000.0 * (paper_exit / expected_exit - 1.0) if expected_exit > 0 else None,
            "exit_fee": exit_fee,
            "paper_pnl_usdt": pnl,
            "paper_return_pct_on_notional": 100.0 * pnl / max(float(trade["notional"]), 1e-12),
            "exit_reason": exit_reason,
            "history_exit": history_row,
            "exit_market_context": context,
        }
    )
    return closed


def should_fill_level(side: str, mark: Optional[float], level: float) -> bool:
    if mark is None or mark <= 0:
        return False
    return mark <= level if side == "LONG" else mark >= level


def trade_key(pos: Dict[str, Any]) -> str:
    return f"{pos['symbol']}:{pos['side']}"


def poll_once(args: argparse.Namespace) -> Dict[str, Any]:
    poll_started = time.time()
    now = utc_now()
    out_dir = Path(args.out_dir)
    state_path = Path(args.state_path) if args.state_path else out_dir / "state.json"
    status_path = out_dir / "PAPER_LIVE_STATUS.json"
    telemetry_path = Path(args.telemetry_path) if args.telemetry_path else out_dir / "telemetry.jsonl"
    out_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    state = load_json(state_path, default_state(args))
    state.setdefault("open_trades", {})
    state.setdefault("closed_trades", [])
    state.setdefault("events", [])
    state.setdefault("equity", args.initial_equity)

    open_positions, positions_meta = fetch_open_positions(session, args.portfolio_id, args.timeout_sec)
    open_positions = [
        p
        for p in open_positions
        if p.get("symbol") == args.symbol and p.get("side") in {"LONG", "SHORT"} and (not args.long_only or p.get("side") == "LONG")
    ]
    history, history_meta = fetch_position_history(
        session,
        args.portfolio_id,
        args.timeout_sec,
        page_size=args.history_page_size,
    )
    market = fetch_market_context(session, args.symbol, args.timeout_sec)
    rules = fetch_symbol_rules(session, args.symbol, args.timeout_sec)
    mark = market.get("mark") or market.get("mid")
    current = {trade_key(pos): pos for pos in open_positions}
    open_trades: Dict[str, Dict[str, Any]] = state["open_trades"]
    events: List[Dict[str, Any]] = []

    for key, pos in sorted(current.items()):
        side = str(pos["side"])
        lead_entry = float(pos["entry_price"])
        if not mark or mark <= 0:
            events.append({"type": "missing_mark", "key": key, "symbol": pos["symbol"], "side": side, "source": market.get("source")})
            continue
        if key not in open_trades:
            plan = fill_plan(float(state["equity"]), args, side, float(mark))
            paper_entry = paper_fill_price(side, float(mark), "entry", SLIPPAGE_RATE)
            trade = {
                "key": key,
                "lead_position_id": pos.get("id"),
                "symbol": pos["symbol"],
                "side": side,
                "opened_at_utc": iso(now),
                "detected_at_ms": int(now.timestamp() * 1000),
                "lead_entry_price": lead_entry,
                "expected_entry_price": float(mark),
                "entry_price_source": market.get("source"),
                "entry_market_context": market,
                "target_notional": plan.target_notional,
                "base_notional": plan.base_notional,
                "add_notionals": list(plan.add_notionals),
                "levels": list(plan.levels),
                "next_level_idx": 0,
                "qty": 0.0,
                "notional": 0.0,
                "avg_entry": 0.0,
                "fees_paid": 0.0,
                "last_seen_utc": iso(now),
                "last_mark": mark,
                "raw_entry_position": pos.get("raw"),
            }
            fill = add_fill(
                trade,
                now=now,
                expected_price=float(mark),
                paper_price=paper_entry,
                notional=plan.base_notional,
                fill_type="base_entry",
                fill_reason="binance_lead_open_position_detected_bingx_mark_snapshot",
                equity_before=float(state["equity"]),
                context=market,
                rules=rules,
            )
            open_trades[key] = trade
            events.append({"type": "paper_entry", "key": key, "fill": fill})
            append_jsonl(telemetry_path, {"event": "fill", "trade_key": key, **fill})
        else:
            trade = open_trades[key]
            trade["last_seen_utc"] = iso(now)
            trade["last_mark"] = mark
            trade["last_raw_position"] = pos.get("raw")

        trade = open_trades[key]
        while int(trade.get("next_level_idx", 0)) < len(trade.get("levels", [])):
            idx = int(trade.get("next_level_idx", 0))
            level = float(trade["levels"][idx])
            if not should_fill_level(str(trade["side"]), mark, level):
                break
            notional = float(trade["add_notionals"][idx])
            paper_price = paper_fill_price(str(trade["side"]), level, "entry", SLIPPAGE_RATE)
            fill = add_fill(
                trade,
                now=now,
                expected_price=level,
                paper_price=paper_price,
                notional=notional,
                fill_type=f"dca_add_{idx + 1}",
                fill_reason="live_mark_crossed_grounded_compound_dca_level",
                equity_before=float(state["equity"]),
                context=market,
                rules=rules,
            )
            trade["next_level_idx"] = idx + 1
            events.append({"type": "paper_dca_fill", "key": key, "fill": fill})
            append_jsonl(telemetry_path, {"event": "fill", "trade_key": key, **fill})

    keys_to_close = set(open_trades) - set(current)

    for key in sorted(keys_to_close):
        trade = open_trades[key]
        hist = find_history_exit(trade, history)
        expected_exit = float(mark or trade.get("last_mark") or 0.0)
        if expected_exit <= 0:
            events.append({"type": "missing_exit_mark", "key": key, "symbol": trade.get("symbol"), "source": market.get("source")})
            continue
        exit_reason = "lead_position_no_longer_open_bingx_mark_snapshot"
        closed = close_trade(
            trade,
            now=now,
            expected_exit=expected_exit,
            exit_reason=exit_reason,
            history_row=hist,
            context=market,
        )
        state["equity"] = float(state["equity"]) + float(closed["paper_pnl_usdt"])
        state["closed_trades"].append(closed)
        del open_trades[key]
        event = {"type": "paper_exit", "key": key, "pnl": closed["paper_pnl_usdt"], "equity": state["equity"]}
        events.append(event)
        append_jsonl(telemetry_path, {"event": "exit", "trade_key": key, **closed})

    open_summary = []
    for key, trade in sorted(open_trades.items()):
        upnl = unrealized_pnl(trade, mark)
        open_summary.append(
            {
                "key": key,
                "symbol": trade["symbol"],
                "side": trade["side"],
                "avg_entry": trade["avg_entry"],
                "notional": trade["notional"],
                "fills": len(trade.get("fills", [])),
                "next_level_idx": trade.get("next_level_idx", 0),
                "last_mark": mark,
                "unrealized_pnl_usdt": upnl,
                "unrealized_pnl_pct_on_notional": 100.0 * upnl / max(float(trade["notional"]), 1e-12),
            }
        )

    latency_ms = (time.time() - poll_started) * 1000.0
    state["last_poll"] = {
        "utc": iso(now),
        "latency_ms": round(latency_ms, 3),
        "timestamp_drift_latency": {
            "poll_started_epoch_ms": int(poll_started * 1000),
            "poll_completed_epoch_ms": int(time.time() * 1000),
            "local_utc": iso(utc_now()),
            "latency_ms": round(latency_ms, 3),
        },
        "positions_meta": positions_meta,
        "history_meta": history_meta,
        "events": events,
        "market_context": market,
        "symbol_rules": rules,
        "paper_only": True,
        "signal_source": "Binance copy-trading current open positions tab/public endpoint",
    }
    state["events"].extend({"utc": iso(now), **event} for event in events)
    state["events"] = state["events"][-args.max_events :]
    status = {
        "utc": iso(now),
        "paper_only": True,
        "portfolio_id": args.portfolio_id,
        "symbol": args.symbol,
        "champion": CHAMPION_NAME,
        "state_path": str(state_path),
        "telemetry_path": str(telemetry_path),
        "equity": state["equity"],
        "open_positions_seen": len(open_positions),
        "open_paper_trades": open_summary,
        "closed_paper_trades": len(state["closed_trades"]),
        "events": events,
        "market_context": market,
    }
    if not args.dry_run:
        write_json(state_path, state)
        write_json(status_path, status)
        append_jsonl(telemetry_path, {"event": "poll", "status": status, "last_poll": state["last_poll"]})
    return status


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Paper-live grounded HYPE compound champion runner.")
    ap.add_argument("--portfolio-id", default=DEFAULT_PORTFOLIO_ID)
    ap.add_argument("--symbol", default=DEFAULT_SYMBOL)
    ap.add_argument("--out-dir", default=str(DEFAULT_REPORT_DIR))
    ap.add_argument("--state-path", default="")
    ap.add_argument("--telemetry-path", default="")
    ap.add_argument("--initial-equity", type=float, default=INITIAL_EQUITY)
    ap.add_argument("--initial-target-notional", type=float, default=INITIAL_TARGET_NOTIONAL)
    ap.add_argument("--history-page-size", type=int, default=50)
    ap.add_argument("--timeout-sec", type=float, default=20.0)
    ap.add_argument("--interval-sec", type=float, default=60.0)
    ap.add_argument("--max-events", type=int, default=2000)
    ap.add_argument("--long-only", action="store_true", default=True)
    ap.add_argument("--dry-run", action="store_true")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--loop", action="store_true")
    mode.add_argument("--once", action="store_true")
    return ap


def main() -> None:
    args = build_arg_parser().parse_args()
    if args.initial_equity <= 0 or args.initial_target_notional <= 0:
        raise SystemExit("initial equity and target notional must be positive")
    if args.interval_sec <= 0:
        raise SystemExit("--interval-sec must be positive")
    while True:
        print(json.dumps(poll_once(args), ensure_ascii=False, indent=2, sort_keys=True), flush=True)
        if not args.loop:
            break
        time.sleep(args.interval_sec)


if __name__ == "__main__":
    main()
