#!/usr/bin/env python3
"""Paper-live Binance online copytrading meta-strategy.

This is a read-only research runner. It polls public Binance copy-trading
endpoints and writes local paper state. It never sends exchange orders.
"""
import argparse
import json
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests


POSITIONS_URL = "https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade/lead-data/positions"
POSITION_HISTORY_URL = "https://www.binance.com/bapi/futures/v1/public/future/copy-trade/lead-portfolio/position-history"
BINANCE_MARK_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
DEFAULT_CONFIG = "obw_platform/meta_strategies/binance_online_copytrading/config.json"
DEFAULT_STATE = "obw_platform/meta_strategies/binance_online_copytrading/reports/state.json"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(raw: Any) -> datetime:
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).astimezone(timezone.utc)
    except AttributeError:
        pass

    offset = timezone.utc
    body = text
    if len(text) >= 6 and text[-6] in {"+", "-"} and text[-3] == ":":
        sign = 1 if text[-6] == "+" else -1
        hours = int(text[-5:-3])
        minutes = int(text[-2:])
        offset = timezone(sign * timedelta(hours=hours, minutes=minutes))
        body = text[:-6]
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(body, fmt).replace(tzinfo=offset).astimezone(timezone.utc)
        except ValueError:
            continue
    raise ValueError("invalid iso datetime: %r" % (raw,))


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


def finite_price(raw: Any) -> Optional[float]:
    val = parse_float(raw)
    return val if math.isfinite(val) and val > 0 else None


def norm_side(raw: Any, amount: float = 0.0) -> str:
    side = str(raw or "").strip().upper()
    if side in {"LONG", "SHORT"}:
        return side
    if side == "BOTH":
        return "LONG" if amount >= 0 else "SHORT"
    if side == "BUY":
        return "LONG"
    if side == "SELL":
        return "SHORT"
    return "UNKNOWN"


def opposite(side: str) -> str:
    return "SHORT" if side == "LONG" else "LONG"


def ret_for(side: str, entry: float, exit_px: float) -> float:
    if entry <= 0 or exit_px <= 0:
        return 0.0
    if side == "LONG":
        return exit_px / entry - 1.0
    if side == "SHORT":
        return entry / exit_px - 1.0
    return 0.0


def exec_price(mark: float, side: str, action: str, slippage_bp: float) -> float:
    slip = slippage_bp / 10_000.0
    if side == "LONG":
        return mark * (1.0 + slip) if action == "entry" else mark * (1.0 - slip)
    return mark * (1.0 - slip) if action == "entry" else mark * (1.0 + slip)


def headers(portfolio_id: str) -> Dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "Origin": "https://www.binance.com",
        "Referer": f"https://www.binance.com/en/copy-trading/lead-details/{portfolio_id}",
    }


def request_json(
    session: requests.Session,
    method: str,
    url: str,
    portfolio_id: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    payload: Optional[Dict[str, Any]] = None,
    timeout_sec: float,
) -> Dict[str, Any]:
    if method == "GET":
        resp = session.get(url, params=params, headers=headers(portfolio_id), timeout=timeout_sec)
    else:
        resp = session.post(url, json=payload, headers=headers(portfolio_id), timeout=timeout_sec)
    resp.raise_for_status()
    data = resp.json()
    if str(data.get("code")) != "000000":
        raise RuntimeError(f"Binance code={data.get('code')} message={data.get('message')}")
    return data


def fetch_open_positions(session: requests.Session, portfolio_id: str, timeout_sec: float) -> List[Dict[str, Any]]:
    raw = request_json(
        session,
        "GET",
        POSITIONS_URL,
        portfolio_id,
        params={"portfolioId": portfolio_id},
        timeout_sec=timeout_sec,
    )
    rows = raw.get("data") or []
    out: List[Dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        amount = parse_float(row.get("positionAmount"), 0.0)
        notional = abs(parse_float(row.get("notionalValue"), 0.0))
        if abs(amount) <= 0 and notional <= 0:
            continue
        symbol = str(row.get("symbol") or "").upper().strip()
        side = norm_side(row.get("positionSide"), amount)
        entry = finite_price(row.get("entryPrice")) or finite_price(row.get("breakEvenPrice"))
        if not symbol or side not in {"LONG", "SHORT"} or not entry:
            continue
        out.append(
            {
                "id": str(row.get("id") or f"{symbol}:{side}"),
                "symbol": symbol,
                "side": side,
                "entry_price": entry,
                "mark_price": finite_price(row.get("markPrice")),
                "position_amount": amount,
                "notional_value": parse_float(row.get("notionalValue"), 0.0),
                "raw": row,
            }
        )
    return out


def fetch_history(
    session: requests.Session,
    portfolio_id: str,
    timeout_sec: float,
    *,
    page_size: int,
    max_pages: int,
) -> List[Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for page in range(1, max_pages + 1):
        payload = {"portfolioId": portfolio_id, "pageNumber": page, "pageSize": page_size, "timeRange": "365D"}
        raw = request_json(session, "POST", POSITION_HISTORY_URL, portfolio_id, payload=payload, timeout_sec=timeout_sec)
        data = raw.get("data") or {}
        rows = data.get("list") or []
        total = int(data.get("total") or len(rows))
        if not isinstance(rows, list) or not rows:
            break
        for row in rows:
            symbol = str(row.get("symbol") or "").upper().strip()
            side = norm_side(row.get("side"))
            avg_close = finite_price(row.get("avgClosePrice"))
            closed_ms = int(row.get("closed") or row.get("updateTime") or 0)
            row_id = str(row.get("id") or "")
            if symbol and side in {"LONG", "SHORT"} and avg_close and closed_ms > 0 and row_id:
                out[row_id] = {
                    "id": row_id,
                    "symbol": symbol,
                    "side": side,
                    "closed_ms": closed_ms,
                    "closed_utc": ms_to_iso(closed_ms),
                    "avg_close_price": avg_close,
                    "avg_cost": finite_price(row.get("avgCost")),
                    "closing_pnl": parse_float(row.get("closingPnl"), 0.0),
                    "raw": row,
                }
        if page * page_size >= total:
            break
    return sorted(out.values(), key=lambda r: int(r["closed_ms"]))


class MarkProvider:
    def __init__(self, exchange: str, timeout_sec: float):
        self.exchange = exchange.lower()
        self.timeout_sec = timeout_sec
        self.ccxt_ex = None
        if self.exchange == "bingx":
            try:
                import ccxt  # type: ignore

                self.ccxt_ex = ccxt.bingx({"enableRateLimit": True})
                self.ccxt_ex.load_markets()
            except Exception:
                self.ccxt_ex = None

    @staticmethod
    def market_symbol(symbol: str) -> str:
        base = symbol[:-4] if symbol.endswith("USDT") else symbol
        return f"{base}/USDT:USDT"

    def mark(self, session: requests.Session, symbol: str, fallback: Optional[float] = None) -> Tuple[Optional[float], str]:
        if self.ccxt_ex is not None:
            market = self.market_symbol(symbol)
            try:
                if market in self.ccxt_ex.markets:
                    ticker = self.ccxt_ex.fetch_ticker(market)
                    price = finite_price(ticker.get("last")) or finite_price(ticker.get("mark"))
                    if price:
                        return price, "bingx_ccxt"
            except Exception:
                pass
        if fallback:
            return fallback, "binance_position_fallback"
        try:
            resp = session.get(BINANCE_MARK_URL, params={"symbol": symbol}, timeout=self.timeout_sec)
            resp.raise_for_status()
            price = finite_price(resp.json().get("markPrice"))
            if price:
                return price, "binance_mark_fallback"
        except Exception:
            pass
        return None, "missing"


def default_state() -> Dict[str, Any]:
    return {"open_positions": {}, "closed_trades": [], "seen_history_ids": {}, "events": [], "last_poll": None}


def load_state(path: Path) -> Dict[str, Any]:
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
    else:
        state = default_state()
    state.setdefault("open_positions", {})
    state.setdefault("closed_trades", [])
    state.setdefault("seen_history_ids", {})
    state.setdefault("events", [])
    return state


def close_paper(
    trade: Dict[str, Any],
    *,
    exit_mark: float,
    exit_source: str,
    exit_reason: str,
    now: datetime,
    slippage_bp: float,
) -> Dict[str, Any]:
    side = str(trade["side"])
    exit_px = exec_price(exit_mark, side, "exit", slippage_bp)
    entry_px = float(trade["entry_exec_price"])
    notional = float(trade["notional_usdt"])
    pnl = ret_for(side, entry_px, exit_px) * notional
    closed = dict(trade)
    closed.update(
        {
            "exit_detected_utc": iso(now),
            "exit_mark_price": exit_mark,
            "exit_exec_price": exit_px,
            "exit_price_source": exit_source,
            "exit_reason": exit_reason,
            "paper_pnl_usdt": pnl,
            "paper_return_pct": 100.0 * pnl / max(notional, 1e-12),
        }
    )
    return closed


def open_paper(
    *,
    strategy_name: str,
    portfolio_id: str,
    mode: str,
    signal_id: str,
    symbol: str,
    side: str,
    mark: float,
    mark_source: str,
    now: datetime,
    notional: float,
    slippage_bp: float,
    ttl_hours: float,
    raw_signal: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "key": f"{strategy_name}:{signal_id}:{symbol}:{side}",
        "strategy_name": strategy_name,
        "portfolio_id": portfolio_id,
        "mode": mode,
        "signal_id": signal_id,
        "symbol": symbol,
        "side": side,
        "detected_utc": iso(now),
        "entry_mark_price": mark,
        "entry_exec_price": exec_price(mark, side, "entry", slippage_bp),
        "entry_price_source": mark_source,
        "notional_usdt": notional,
        "ttl_until_utc": iso(now + timedelta(hours=ttl_hours)),
        "raw_signal": raw_signal,
    }


def apply_follow_open(
    state: Dict[str, Any],
    *,
    lead: Dict[str, Any],
    open_positions: List[Dict[str, Any]],
    history: List[Dict[str, Any]],
    mark_provider: MarkProvider,
    session: requests.Session,
    now: datetime,
    notional: float,
    slippage_bp: float,
    ttl_hours: float,
) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    current_keys = set()
    for pos in open_positions:
        signal_id = str(pos.get("id") or f"{pos['symbol']}:{pos['side']}")
        key = f"{lead['name']}:{signal_id}:{pos['symbol']}:{pos['side']}"
        current_keys.add(key)
        if key in state["open_positions"]:
            state["open_positions"][key]["last_seen_utc"] = iso(now)
            continue
        mark, source = mark_provider.mark(session, pos["symbol"], pos.get("mark_price") or pos.get("entry_price"))
        if not mark:
            events.append({"type": "missing_mark", "strategy": lead["name"], "symbol": pos["symbol"]})
            continue
        trade = open_paper(
            strategy_name=lead["name"],
            portfolio_id=lead["portfolio_id"],
            mode="follow_open",
            signal_id=signal_id,
            symbol=pos["symbol"],
            side=pos["side"],
            mark=mark,
            mark_source=source,
            now=now,
            notional=notional,
            slippage_bp=slippage_bp,
            ttl_hours=ttl_hours,
            raw_signal=pos,
        )
        state["open_positions"][key] = trade
        events.append({"type": "paper_entry", "key": key, "strategy": lead["name"], "symbol": pos["symbol"], "side": pos["side"], "mark": mark})

    for key, trade in list(state["open_positions"].items()):
        if trade.get("strategy_name") != lead["name"] or trade.get("mode") != "follow_open" or key in current_keys:
            continue
        symbol = str(trade["symbol"])
        hist = next((h for h in reversed(history) if h["symbol"] == symbol and h["side"] == trade["side"]), None)
        fallback = hist.get("avg_close_price") if hist else None
        mark, source = mark_provider.mark(session, symbol, fallback)
        if not mark:
            continue
        closed = close_paper(trade, exit_mark=mark, exit_source=source, exit_reason="lead_position_no_longer_open_market_snapshot", now=now, slippage_bp=slippage_bp)
        closed["history_exit"] = hist
        state["closed_trades"].append(closed)
        del state["open_positions"][key]
        events.append({"type": "paper_exit", "key": key, "strategy": lead["name"], "symbol": symbol, "pnl": closed["paper_pnl_usdt"]})
    return events


def apply_contrarian_on_close(
    state: Dict[str, Any],
    *,
    lead: Dict[str, Any],
    history: List[Dict[str, Any]],
    mark_provider: MarkProvider,
    session: requests.Session,
    now: datetime,
    notional: float,
    slippage_bp: float,
    ttl_hours: float,
    exit_on_reversal: bool,
    trade_existing_history: bool,
) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    seen = state["seen_history_ids"].setdefault(lead["name"], [])
    if not seen and not trade_existing_history:
        seen.extend(row["id"] for row in history)
        state["seen_history_ids"][lead["name"]] = seen[-1000:]
        return [{"type": "seed_history", "strategy": lead["name"], "seeded_rows": len(history)}]
    seen_set = set(seen)

    for key, trade in list(state["open_positions"].items()):
        if trade.get("strategy_name") != lead["name"] or trade.get("mode") != "contrarian_on_close":
            continue
        ttl_hit = now >= parse_iso(trade["ttl_until_utc"])
        reversal = None
        if exit_on_reversal:
            for row in history:
                if row["id"] == trade.get("signal_id"):
                    continue
                if row["symbol"] == trade["symbol"] and int(row["closed_ms"]) > int(trade.get("signal_closed_ms", 0)):
                    reversal = row
                    break
        if not ttl_hit and not reversal:
            continue
        fallback = reversal.get("avg_close_price") if reversal else None
        mark, source = mark_provider.mark(session, str(trade["symbol"]), fallback)
        if not mark:
            continue
        reason = "same_symbol_reversal" if reversal else "ttl"
        closed = close_paper(trade, exit_mark=mark, exit_source=source, exit_reason=reason, now=now, slippage_bp=slippage_bp)
        closed["reversal_signal"] = reversal
        state["closed_trades"].append(closed)
        del state["open_positions"][key]
        events.append({"type": "paper_exit", "key": key, "strategy": lead["name"], "symbol": trade["symbol"], "reason": reason, "pnl": closed["paper_pnl_usdt"]})

    for row in history:
        if row["id"] in seen_set:
            continue
        seen.append(row["id"])
        side = opposite(row["side"])
        mark = row["avg_close_price"]
        trade = open_paper(
            strategy_name=lead["name"],
            portfolio_id=lead["portfolio_id"],
            mode="contrarian_on_close",
            signal_id=row["id"],
            symbol=row["symbol"],
            side=side,
            mark=mark,
            mark_source="binance_position_history_avg_close",
            now=now,
            notional=notional,
            slippage_bp=slippage_bp,
            ttl_hours=ttl_hours,
            raw_signal=row,
        )
        trade["signal_closed_ms"] = row["closed_ms"]
        state["open_positions"][trade["key"]] = trade
        events.append({"type": "paper_entry", "key": trade["key"], "strategy": lead["name"], "symbol": row["symbol"], "side": side, "mark": mark})
    state["seen_history_ids"][lead["name"]] = seen[-1000:]
    return events


def poll_once(args: argparse.Namespace) -> Dict[str, Any]:
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    state_path = Path(args.state_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = load_state(state_path)
    session = requests.Session()
    mark_provider = MarkProvider(args.paper_exchange, args.timeout_sec)
    now = utc_now()
    events: List[Dict[str, Any]] = []
    lead_meta: List[Dict[str, Any]] = []
    notional = float(args.notional_usdt or cfg.get("paper_notional_usdt", 100.0))
    slippage_bp = float(args.slippage_bp if args.slippage_bp is not None else cfg.get("slippage_bp", 9.38))
    ttl_hours = float(args.ttl_hours or cfg.get("ttl_hours", 72.0))
    exit_on_reversal = bool(cfg.get("exit_on_reversal", True))
    if args.no_exit_on_reversal:
        exit_on_reversal = False

    for lead in cfg.get("leads", []):
        if not lead.get("enabled", True):
            continue
        portfolio_id = str(lead["portfolio_id"])
        mode = str(lead["mode"])
        open_positions = fetch_open_positions(session, portfolio_id, args.timeout_sec) if mode == "follow_open" else []
        history = fetch_history(session, portfolio_id, args.timeout_sec, page_size=args.history_page_size, max_pages=args.history_pages)
        if mode == "follow_open":
            lead_events = apply_follow_open(
                state,
                lead=lead,
                open_positions=open_positions,
                history=history,
                mark_provider=mark_provider,
                session=session,
                now=now,
                notional=notional,
                slippage_bp=slippage_bp,
                ttl_hours=ttl_hours,
            )
        elif mode == "contrarian_on_close":
            lead_events = apply_contrarian_on_close(
                state,
                lead=lead,
                history=history,
                mark_provider=mark_provider,
                session=session,
                now=now,
                notional=notional,
                slippage_bp=slippage_bp,
                ttl_hours=ttl_hours,
                exit_on_reversal=exit_on_reversal,
                trade_existing_history=args.trade_existing_history,
            )
        else:
            raise RuntimeError(f"unknown lead mode: {mode}")
        events.extend(lead_events)
        lead_meta.append({"name": lead["name"], "portfolio_id": portfolio_id, "mode": mode, "open_rows": len(open_positions), "history_rows": len(history), "events": len(lead_events)})

    state["last_poll"] = {"utc": iso(now), "paper_exchange": args.paper_exchange, "slippage_bp": slippage_bp, "ttl_hours": ttl_hours, "leads": lead_meta, "events": events}
    state["events"].extend({"utc": iso(now), **event} for event in events)
    state["events"] = state["events"][-args.max_events :]
    if not args.dry_run:
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "dry_run": args.dry_run,
        "state_path": str(state_path),
        "open_paper_positions": len(state["open_positions"]),
        "closed_paper_trades": len(state["closed_trades"]),
        "events": events,
        "leads": lead_meta,
    }


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Paper-live Binance online copytrading meta-strategy.")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--state-path", default=DEFAULT_STATE)
    ap.add_argument("--paper-exchange", default="bingx", choices=["bingx", "binance"])
    ap.add_argument("--notional-usdt", type=float, default=0.0)
    ap.add_argument("--slippage-bp", type=float, default=None)
    ap.add_argument("--ttl-hours", type=float, default=0.0)
    ap.add_argument("--history-page-size", type=int, default=50)
    ap.add_argument("--history-pages", type=int, default=2)
    ap.add_argument("--timeout-sec", type=float, default=20.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-exit-on-reversal", action="store_true")
    ap.add_argument("--trade-existing-history", action="store_true", help="On first run, trade already visible history rows instead of seeding them as seen.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    ap.add_argument("--interval-sec", type=float, default=60.0)
    ap.add_argument("--max-events", type=int, default=2000)
    return ap


def main() -> None:
    args = build_parser().parse_args()
    while True:
        print(json.dumps(poll_once(args), ensure_ascii=False, indent=2))
        if not args.loop:
            break
        time.sleep(args.interval_sec)


if __name__ == "__main__":
    main()
