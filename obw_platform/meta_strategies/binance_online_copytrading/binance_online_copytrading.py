#!/usr/bin/env python3
"""Paper-live Binance online copytrading meta-strategy.

This is a read-only research runner. It polls public Binance copy-trading
endpoints and writes local paper state. It never sends exchange orders.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import json
import math
import sqlite3
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import yaml


POSITIONS_URL = "https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade/lead-data/positions"
POSITION_HISTORY_URL = "https://www.binance.com/bapi/futures/v1/public/future/copy-trade/lead-portfolio/position-history"
BINANCE_MARK_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
DEFAULT_CONFIG = "obw_platform/meta_strategies/binance_online_copytrading/config.json"
DEFAULT_STATE = "obw_platform/meta_strategies/binance_online_copytrading/reports/state.json"
DEFAULT_SESSION_DB = "obw_platform/meta_strategies/binance_online_copytrading/reports/session.sqlite"
DEFAULT_SHADOW_ORDERS = "obw_platform/meta_strategies/binance_online_copytrading/reports/shadow_orders.jsonl"
DEFAULT_V21_CONFIG = "obw_platform/configs/V21_strict_trend_stable_live_static9p38.yaml"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(raw: Any) -> datetime:
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(timezone.utc)


def stable_id(*parts: Any) -> str:
    raw = "|".join(str(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


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
        if self.exchange == "bingx":
            return None, "missing_bingx_mark"
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
    return {"open_positions": {}, "closed_trades": [], "seen_history_ids": {}, "events": [], "lead_traders": {}, "last_poll": None}


def load_state(path: Path) -> Dict[str, Any]:
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
    else:
        state = default_state()
    state.setdefault("open_positions", {})
    state.setdefault("closed_trades", [])
    state.setdefault("seen_history_ids", {})
    state.setdefault("events", [])
    state.setdefault("lead_traders", {})
    return state


def lead_trader_name(lead: Dict[str, Any]) -> str:
    return str(lead.get("lead_trader_name") or lead.get("trader_name") or lead.get("nickname") or lead.get("name") or "")


def enrich_existing_trades_with_lead_name(state: Dict[str, Any], lead: Dict[str, Any]) -> None:
    name = lead_trader_name(lead)
    if not name:
        return
    state.setdefault("lead_traders", {})[str(lead["portfolio_id"])] = {
        "portfolio_id": str(lead["portfolio_id"]),
        "strategy_name": str(lead["name"]),
        "lead_trader_name": name,
    }
    for trade in (state.get("open_positions") or {}).values():
        if trade.get("strategy_name") == lead.get("name") or str(trade.get("portfolio_id")) == str(lead.get("portfolio_id")):
            trade["lead_trader_name"] = name
    for trade in state.get("closed_trades", []):
        if trade.get("strategy_name") == lead.get("name") or str(trade.get("portfolio_id")) == str(lead.get("portfolio_id")):
            trade["lead_trader_name"] = name


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


class PaperV21Position:
    def __init__(self, entry: float, qty: float):
        self.entry = float(entry)
        self.qty = float(qty)


def _import_class(class_path: str):
    mod_path, cls_name = str(class_path).rsplit(".", 1)
    obw_root = Path(__file__).resolve().parents[2]
    if str(obw_root) not in sys.path:
        sys.path.insert(0, str(obw_root))
    mod = importlib.import_module(mod_path)
    return getattr(mod, cls_name)


def load_v21_runtime_cfg(path: str, side: str, delegated_capital_usdt: float) -> Dict[str, Any]:
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    cfg = copy.deepcopy(cfg)
    side = str(side).upper()
    params_key = "strategy_params_long" if side == "LONG" else "strategy_params_short"
    params = cfg.setdefault(params_key, {})
    params["equityForSizingUSDT"] = float(delegated_capital_usdt)
    params["baseOrderPctEq"] = 5.0
    cfg.setdefault("portfolio", {})
    return cfg


def v21_row(now: datetime, price: float) -> Dict[str, Any]:
    return {
        "datetime_utc": iso(now),
        "open": float(price),
        "high": float(price),
        "low": float(price),
        "close": float(price),
        "atr_ratio": 0.0,
        "gain_24h_before": 0.0,
        "dp6h": 0.0,
        "vol_surge_mult": 0.0,
    }


def _snapshot_to_json(snapshot: Any) -> Optional[Dict[str, Any]]:
    if snapshot is None:
        return None
    out: Dict[str, Any] = {}
    for key, value in vars(snapshot).items():
        if isinstance(value, deque):
            out[key] = list(value)
        elif key == "lots" and value is not None:
            out[key] = [[float(q), float(px)] for q, px in value]
        else:
            out[key] = value
    return out


def _restore_json_snapshot(strategy: Any, symbol: str, payload: Optional[Dict[str, Any]]) -> None:
    if not payload:
        return
    state = strategy._get_state(symbol)
    deque_keys = {"trend_htf_closes", "trend_ma_series", "rets_short", "rets_long"}
    for key, value in payload.items():
        if key in deque_keys:
            setattr(state, key, deque(value or []))
        elif key == "lots" and value is not None:
            setattr(state, key, [(float(q), float(px)) for q, px in value])
        else:
            setattr(state, key, value)


class V21OneLegRuntime:
    """Tiny read-only adapter around the real V21 one-leg strategy class."""

    def __init__(self, cfg_path: str, delegated_capital_usdt: float):
        self.cfg_path = cfg_path
        self.delegated_capital_usdt = float(delegated_capital_usdt)

    def _strategy(self, side: str):
        side = str(side).upper()
        cfg = load_v21_runtime_cfg(self.cfg_path, side, self.delegated_capital_usdt)
        class_key = "strategy_class_long" if side == "LONG" else "strategy_class_short"
        cls = _import_class(str(cfg[class_key]))
        return cls(cfg), cfg, str(cfg[class_key])

    def open_signal(self, *, symbol: str, side: str, mark: float, now: datetime) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        strategy, cfg, class_path = self._strategy(side)
        row = v21_row(now, mark)
        sig = strategy.entry_signal(True, symbol, row, ctx={"source": "binance_copy"})
        if sig is None:
            return None, "v21_entry_filtered"
        qty = float(getattr(sig, "qty", 0.0) or 0.0)
        if qty <= 0:
            return None, "v21_entry_zero_qty"
        snapshot = _snapshot_to_json(strategy.export_state_snapshot(symbol))
        return (
            {
                "enabled": True,
                "cfg_path": self.cfg_path,
                "class_path": class_path,
                "rollback_label": str(cfg.get("rollback_label", "")),
                "delegated_capital_usdt": self.delegated_capital_usdt,
                "base_order_pct_eq": 5.0,
                "qty": qty,
                "tp": getattr(sig, "tp", None),
                "sl": getattr(sig, "sl", None),
                "reason": getattr(sig, "reason", ""),
                "state": snapshot,
            },
            None,
        )

    def manage(self, trade: Dict[str, Any], *, mark: float, now: datetime) -> Dict[str, Any]:
        side = str(trade.get("side") or "")
        symbol = str(trade.get("symbol") or "")
        v21 = trade.get("v21") or {}
        strategy, _cfg, _class_path = self._strategy(side)
        _restore_json_snapshot(strategy, symbol, v21.get("state"))
        pos = PaperV21Position(float(trade.get("entry_exec_price") or trade.get("entry_mark_price") or mark), float(v21.get("qty") or trade.get("qty") or 0.0))
        before_qty = float(pos.qty)
        before_entry = float(pos.entry)
        sig = strategy.manage_position(symbol, v21_row(now, mark), pos, ctx={"source": "binance_copy"})
        snapshot = _snapshot_to_json(strategy.export_state_snapshot(symbol))
        result: Dict[str, Any] = {
            "state": snapshot,
            "qty": float(pos.qty),
            "entry": float(pos.entry),
            "changed": False,
            "exit": None,
        }
        if abs(float(pos.qty) - before_qty) > 1e-12 or abs(float(pos.entry) - before_entry) > 1e-12:
            result["changed"] = True
            result["reason"] = "v21_position_adjust"
        if sig is not None:
            result["exit"] = {
                "action": getattr(sig, "action", ""),
                "exit_price": float(getattr(sig, "exit_price", mark) or mark),
                "qty_frac": float(getattr(sig, "qty_frac", 1.0) or 1.0),
                "reason": getattr(sig, "reason", ""),
            }
        return result


def open_v21_paper(
    *,
    runtime: V21OneLegRuntime,
    strategy_name: str,
    lead_name: str,
    portfolio_id: str,
    mode: str,
    signal_id: str,
    symbol: str,
    side: str,
    mark: float,
    mark_source: str,
    now: datetime,
    slippage_bp: float,
    ttl_hours: float,
    raw_signal: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    entry_exec = exec_price(mark, side, "entry", slippage_bp)
    v21, skip_reason = runtime.open_signal(symbol=symbol, side=side, mark=entry_exec, now=now)
    if v21 is None:
        return None, skip_reason
    qty = float(v21["qty"])
    trade = open_paper(
        strategy_name=strategy_name,
        lead_name=lead_name,
        portfolio_id=portfolio_id,
        mode=mode,
        signal_id=signal_id,
        symbol=symbol,
        side=side,
        mark=mark,
        mark_source=mark_source,
        now=now,
        notional=qty * entry_exec,
        slippage_bp=slippage_bp,
        ttl_hours=ttl_hours,
        raw_signal=raw_signal,
    )
    trade["qty"] = qty
    trade["v21"] = v21
    return trade, None


def has_open_source_symbol(state: Dict[str, Any], strategy_name: str, symbol: str) -> bool:
    return any(
        trade.get("strategy_name") == strategy_name and trade.get("symbol") == symbol
        for trade in (state.get("open_positions") or {}).values()
    )


def manage_v21_trade(
    trade: Dict[str, Any],
    *,
    runtime: V21OneLegRuntime,
    mark: float,
    mark_source: str,
    now: datetime,
    slippage_bp: float,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    if not trade.get("v21"):
        return None, None
    result = runtime.manage(trade, mark=mark, now=now)
    trade["v21"]["state"] = result.get("state")
    trade["v21"]["qty"] = result.get("qty", trade.get("qty"))
    trade["qty"] = result.get("qty", trade.get("qty"))
    trade["entry_exec_price"] = result.get("entry", trade.get("entry_exec_price"))
    trade["notional_usdt"] = float(trade.get("qty") or 0.0) * float(trade.get("entry_exec_price") or 0.0)
    if result.get("changed"):
        event = {
            "type": "paper_v21_adjust",
            "key": trade.get("key"),
            "strategy": trade.get("strategy_name"),
            "lead_trader_name": trade.get("lead_trader_name"),
            "symbol": trade.get("symbol"),
            "side": trade.get("side"),
            "qty": trade.get("qty"),
            "entry": trade.get("entry_exec_price"),
            "reason": result.get("reason"),
        }
    else:
        event = None
    exit_sig = result.get("exit")
    if exit_sig:
        closed = close_paper(
            trade,
            exit_mark=float(exit_sig.get("exit_price") or mark),
            exit_source=f"v21:{mark_source}",
            exit_reason=f"v21:{exit_sig.get('action')}:{exit_sig.get('reason')}",
            now=now,
            slippage_bp=slippage_bp,
        )
        return event, closed
    return event, None


def open_paper(
    *,
    strategy_name: str,
    lead_name: str,
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
        "lead_trader_name": lead_name,
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
    v21_runtime: V21OneLegRuntime,
    now: datetime,
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
            mark, source = mark_provider.mark(session, pos["symbol"], pos.get("mark_price") or pos.get("entry_price"))
            if mark:
                event, closed = manage_v21_trade(
                    state["open_positions"][key],
                    runtime=v21_runtime,
                    mark=mark,
                    mark_source=source,
                    now=now,
                    slippage_bp=slippage_bp,
                )
                if event:
                    events.append(event)
                if closed:
                    state["closed_trades"].append(closed)
                    del state["open_positions"][key]
                    events.append({"type": "paper_exit", "key": key, "strategy": lead["name"], "lead_trader_name": lead_trader_name(lead), "symbol": pos["symbol"], "reason": closed["exit_reason"], "pnl": closed["paper_pnl_usdt"]})
            continue
        if has_open_source_symbol(state, lead["name"], pos["symbol"]):
            events.append({"type": "skip_signal", "strategy": lead["name"], "lead_trader_name": lead_trader_name(lead), "symbol": pos["symbol"], "side": pos["side"], "reason": "source_symbol_already_active"})
            continue
        mark, source = mark_provider.mark(session, pos["symbol"], pos.get("mark_price") or pos.get("entry_price"))
        if not mark:
            events.append({"type": "missing_mark", "strategy": lead["name"], "lead_trader_name": lead_trader_name(lead), "symbol": pos["symbol"]})
            continue
        trade, skip_reason = open_v21_paper(
            runtime=v21_runtime,
            strategy_name=lead["name"],
            lead_name=lead_trader_name(lead),
            portfolio_id=lead["portfolio_id"],
            mode="follow_open",
            signal_id=signal_id,
            symbol=pos["symbol"],
            side=pos["side"],
            mark=mark,
            mark_source=source,
            now=now,
            slippage_bp=slippage_bp,
            ttl_hours=ttl_hours,
            raw_signal=pos,
        )
        if trade is None:
            events.append({"type": "skip_signal", "strategy": lead["name"], "lead_trader_name": lead_trader_name(lead), "symbol": pos["symbol"], "side": pos["side"], "reason": skip_reason})
            continue
        state["open_positions"][key] = trade
        events.append({"type": "paper_entry", "key": key, "strategy": lead["name"], "lead_trader_name": lead_trader_name(lead), "symbol": pos["symbol"], "side": pos["side"], "mark": mark})

    for key, trade in list(state["open_positions"].items()):
        if trade.get("strategy_name") != lead["name"] or trade.get("mode") != "follow_open" or key in current_keys:
            continue
        symbol = str(trade["symbol"])
        hist = next((h for h in reversed(history) if h["symbol"] == symbol and h["side"] == trade["side"]), None)
        mark, source = mark_provider.mark(session, symbol)
        if not mark:
            continue
        if trade.get("v21"):
            event, v21_closed = manage_v21_trade(trade, runtime=v21_runtime, mark=mark, mark_source=source, now=now, slippage_bp=slippage_bp)
            if event:
                events.append(event)
            if v21_closed:
                state["closed_trades"].append(v21_closed)
                del state["open_positions"][key]
                events.append({"type": "paper_exit", "key": key, "strategy": lead["name"], "lead_trader_name": lead_trader_name(lead), "symbol": symbol, "reason": v21_closed["exit_reason"], "pnl": v21_closed["paper_pnl_usdt"]})
            continue
        closed = close_paper(trade, exit_mark=mark, exit_source=source, exit_reason="lead_position_no_longer_open", now=now, slippage_bp=slippage_bp)
        state["closed_trades"].append(closed)
        del state["open_positions"][key]
        events.append({"type": "paper_exit", "key": key, "strategy": lead["name"], "lead_trader_name": lead_trader_name(lead), "symbol": symbol, "pnl": closed["paper_pnl_usdt"]})
    return events


def apply_contrarian_on_close(
    state: Dict[str, Any],
    *,
    lead: Dict[str, Any],
    history: List[Dict[str, Any]],
    mark_provider: MarkProvider,
    session: requests.Session,
    v21_runtime: V21OneLegRuntime,
    now: datetime,
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
        return [{"type": "seed_history", "strategy": lead["name"], "lead_trader_name": lead_trader_name(lead), "seeded_rows": len(history)}]
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
        mark, source = mark_provider.mark(session, str(trade["symbol"]))
        if not mark:
            continue
        event, v21_closed = manage_v21_trade(trade, runtime=v21_runtime, mark=mark, mark_source=source, now=now, slippage_bp=slippage_bp)
        if event:
            events.append(event)
        if v21_closed:
            v21_closed["reversal_signal"] = reversal
            state["closed_trades"].append(v21_closed)
            del state["open_positions"][key]
            events.append({"type": "paper_exit", "key": key, "strategy": lead["name"], "lead_trader_name": lead_trader_name(lead), "symbol": trade["symbol"], "reason": v21_closed["exit_reason"], "pnl": v21_closed["paper_pnl_usdt"]})
            continue
        if trade.get("v21"):
            continue
        if not ttl_hit and not reversal:
            continue
        reason = "same_symbol_reversal" if reversal else "ttl"
        closed = close_paper(trade, exit_mark=mark, exit_source=source, exit_reason=reason, now=now, slippage_bp=slippage_bp)
        closed["reversal_signal"] = reversal
        state["closed_trades"].append(closed)
        del state["open_positions"][key]
        events.append({"type": "paper_exit", "key": key, "strategy": lead["name"], "lead_trader_name": lead_trader_name(lead), "symbol": trade["symbol"], "reason": reason, "pnl": closed["paper_pnl_usdt"]})

    for row in history:
        if row["id"] in seen_set:
            continue
        seen.append(row["id"])
        side = opposite(row["side"])
        if has_open_source_symbol(state, lead["name"], row["symbol"]):
            events.append({"type": "skip_signal", "strategy": lead["name"], "lead_trader_name": lead_trader_name(lead), "symbol": row["symbol"], "side": side, "reason": "source_symbol_already_active"})
            continue
        mark, source = mark_provider.mark(session, row["symbol"])
        if not mark:
            events.append({"type": "missing_mark", "strategy": lead["name"], "lead_trader_name": lead_trader_name(lead), "symbol": row["symbol"], "side": side, "reason": source})
            continue
        trade, skip_reason = open_v21_paper(
            runtime=v21_runtime,
            strategy_name=lead["name"],
            lead_name=lead_trader_name(lead),
            portfolio_id=lead["portfolio_id"],
            mode="contrarian_on_close",
            signal_id=row["id"],
            symbol=row["symbol"],
            side=side,
            mark=mark,
            mark_source=source,
            now=now,
            slippage_bp=slippage_bp,
            ttl_hours=ttl_hours,
            raw_signal=row,
        )
        if trade is None:
            events.append({"type": "skip_signal", "strategy": lead["name"], "lead_trader_name": lead_trader_name(lead), "symbol": row["symbol"], "side": side, "reason": skip_reason})
            continue
        trade["signal_closed_ms"] = row["closed_ms"]
        state["open_positions"][trade["key"]] = trade
        events.append({"type": "paper_entry", "key": trade["key"], "strategy": lead["name"], "lead_trader_name": lead_trader_name(lead), "symbol": row["symbol"], "side": side, "mark": mark})
    state["seen_history_ids"][lead["name"]] = seen[-1000:]
    return events


def ensure_session_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    cur = con.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute(
        """CREATE TABLE IF NOT EXISTS run_meta(
            run_id TEXT PRIMARY KEY,
            started_utc TEXT,
            last_poll_utc TEXT,
            status TEXT,
            config_json TEXT,
            argv_json TEXT
        )"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS signal_polls(
            poll_utc TEXT PRIMARY KEY,
            run_id TEXT,
            paper_exchange TEXT,
            slippage_bp REAL,
            ttl_hours REAL,
            open_paper_positions INTEGER,
            closed_paper_trades INTEGER,
            lead_json TEXT,
            event_count INTEGER
        )"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS paper_events(
            event_id TEXT PRIMARY KEY,
            run_id TEXT,
            event_utc TEXT,
            event_type TEXT,
            strategy TEXT,
            symbol TEXT,
            side TEXT,
            paper_key TEXT,
            payload_json TEXT
        )"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS paper_positions(
            paper_key TEXT PRIMARY KEY,
            run_id TEXT,
            strategy TEXT,
            portfolio_id TEXT,
            mode TEXT,
            signal_id TEXT,
            symbol TEXT,
            side TEXT,
            lead_trader_name TEXT,
            status TEXT,
            detected_utc TEXT,
            last_seen_utc TEXT,
            entry_mark_price REAL,
            entry_exec_price REAL,
            exit_exec_price REAL,
            notional_usdt REAL,
            paper_pnl_usdt REAL,
            payload_json TEXT
        )"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS shadow_orders(
            shadow_order_id TEXT PRIMARY KEY,
            run_id TEXT,
            created_utc TEXT,
            source TEXT,
            source_id TEXT,
            mode TEXT,
            symbol TEXT,
            side TEXT,
            lead_trader_name TEXT,
            event TEXT,
            price_hint REAL,
            target_notional REAL,
            reason TEXT,
            client_order_id TEXT,
            payload_json TEXT
        )"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS orders(
            order_id TEXT PRIMARY KEY,
            ts_utc TEXT,
            bar_time_utc TEXT,
            mode TEXT,
            symbol TEXT,
            side TEXT,
            type TEXT,
            price REAL,
            qty REAL,
            status TEXT,
            reason TEXT,
            run_id TEXT,
            extra TEXT
        )"""
    )
    for table in ("paper_positions", "shadow_orders"):
        try:
            cols = [r[1] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()]
            if "lead_trader_name" not in cols:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN lead_trader_name TEXT")
        except Exception:
            pass
    cur.execute(
        """CREATE TABLE IF NOT EXISTS equity(
            run_id TEXT,
            ts_utc TEXT,
            equity_usdt REAL,
            cash_usdt REAL,
            position_value_usdt REAL,
            realized_pnl_cum REAL,
            unrealized_pnl REAL,
            PRIMARY KEY(run_id, ts_utc)
        )"""
    )
    con.commit()
    con.close()


def _side_to_order_side(side: str, action: str) -> str:
    side = str(side).upper()
    if action == "entry":
        return "buy" if side == "LONG" else "sell"
    return "sell" if side == "LONG" else "buy"


def _qty_from_notional(notional: float, price: float) -> float:
    if notional <= 0 or price <= 0:
        return 0.0
    return notional / price


def _equity_snapshot(state: Dict[str, Any], initial_equity: float = 0.0) -> Dict[str, float]:
    realized = sum(float(t.get("paper_pnl_usdt") or 0.0) for t in state.get("closed_trades", []))
    position_value = sum(float(t.get("notional_usdt") or 0.0) for t in (state.get("open_positions") or {}).values())
    return {
        "equity": float(initial_equity) + realized,
        "cash": float(initial_equity) + realized,
        "position_value": position_value,
        "realized_pnl_cum": realized,
        "unrealized_pnl": 0.0,
    }


def write_observability(
    *,
    state: Dict[str, Any],
    result: Dict[str, Any],
    cfg: Dict[str, Any],
    args: argparse.Namespace,
    run_id: str,
    now: datetime,
) -> None:
    if args.dry_run or not args.session_db:
        return
    db_path = Path(args.session_db)
    ensure_session_db(db_path)
    poll = state.get("last_poll") or {}
    poll_utc = str(poll.get("utc") or iso(now))
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO run_meta(run_id, started_utc, last_poll_utc, status, config_json, argv_json) VALUES(?,?,?,?,?,?)",
        (
            run_id,
            poll_utc,
            poll_utc,
            "running",
            json.dumps(cfg, ensure_ascii=False, sort_keys=True),
            json.dumps(vars(args), ensure_ascii=False, sort_keys=True),
        ),
    )
    cur.execute(
        """INSERT OR REPLACE INTO signal_polls(
            poll_utc, run_id, paper_exchange, slippage_bp, ttl_hours,
            open_paper_positions, closed_paper_trades, lead_json, event_count
        ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            poll_utc,
            run_id,
            poll.get("paper_exchange"),
            float(poll.get("slippage_bp") or 0.0),
            float(poll.get("ttl_hours") or 0.0),
            int(result.get("open_paper_positions") or 0),
            int(result.get("closed_paper_trades") or 0),
            json.dumps(poll.get("leads") or [], ensure_ascii=False, sort_keys=True),
            len(poll.get("events") or []),
        ),
    )
    for event in poll.get("events") or []:
        event_id = stable_id(poll_utc, event.get("type"), event.get("key"), event.get("strategy"), event.get("symbol"), event.get("side"))
        cur.execute(
            """INSERT OR IGNORE INTO paper_events(
                event_id, run_id, event_utc, event_type, strategy, symbol, side, paper_key, payload_json
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                event_id,
                run_id,
                poll_utc,
                event.get("type"),
                event.get("strategy"),
                event.get("symbol"),
                event.get("side"),
                event.get("key"),
                json.dumps(event, ensure_ascii=False, sort_keys=True),
            ),
        )

    shadow_rows: List[Dict[str, Any]] = []
    for trade in (state.get("open_positions") or {}).values():
        key = str(trade.get("key") or "")
        side = str(trade.get("side") or "")
        price = float(trade.get("entry_exec_price") or trade.get("entry_mark_price") or 0.0)
        notional = float(trade.get("notional_usdt") or 0.0)
        cur.execute(
            """INSERT OR REPLACE INTO paper_positions(
                paper_key, run_id, strategy, portfolio_id, mode, signal_id, symbol, side, status,
                lead_trader_name, detected_utc, last_seen_utc, entry_mark_price, entry_exec_price, exit_exec_price,
                notional_usdt, paper_pnl_usdt, payload_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                key,
                run_id,
                trade.get("strategy_name"),
                trade.get("portfolio_id"),
                trade.get("mode"),
                trade.get("signal_id"),
                trade.get("symbol"),
                side,
                "OPEN",
                trade.get("lead_trader_name"),
                trade.get("detected_utc"),
                trade.get("last_seen_utc"),
                float(trade.get("entry_mark_price") or 0.0),
                price,
                None,
                notional,
                None,
                json.dumps(trade, ensure_ascii=False, sort_keys=True),
            ),
        )
        client_order_id = stable_id("binance_copy", key, "entry")
        shadow = {
            "source": "binance_copy",
            "source_id": key,
            "mode": trade.get("mode"),
            "symbol": MarkProvider.market_symbol(str(trade.get("symbol") or "")),
            "side": side,
            "lead_trader_name": trade.get("lead_trader_name"),
            "event": "entry",
            "price_hint": price,
            "target_notional": notional,
            "reason": "paper_open_position",
            "client_order_id": client_order_id,
            "created_at_utc": poll_utc,
        }
        shadow_rows.append(shadow)
        cur.execute(
            """INSERT OR REPLACE INTO shadow_orders(
                shadow_order_id, run_id, created_utc, source, source_id, mode, symbol, side, lead_trader_name, event,
                price_hint, target_notional, reason, client_order_id, payload_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                stable_id("shadow", client_order_id),
                run_id,
                poll_utc,
                shadow["source"],
                shadow["source_id"],
                shadow["mode"],
                shadow["symbol"],
                shadow["side"],
                shadow["lead_trader_name"],
                shadow["event"],
                shadow["price_hint"],
                shadow["target_notional"],
                shadow["reason"],
                shadow["client_order_id"],
                json.dumps(shadow, ensure_ascii=False, sort_keys=True),
            ),
        )
        cur.execute(
            """INSERT OR REPLACE INTO orders(
                order_id, ts_utc, bar_time_utc, mode, symbol, side, type, price, qty, status, reason, run_id, extra
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                client_order_id,
                poll_utc,
                poll_utc,
                "binance_copy_paper",
                shadow["symbol"],
                _side_to_order_side(side, "entry"),
                "market",
                price,
                _qty_from_notional(notional, price),
                "shadow_open",
                "paper_entry",
                run_id,
                json.dumps({"sim": True, "paper_key": key, "never_submitted": True}, ensure_ascii=False, sort_keys=True),
            ),
        )

    for trade in state.get("closed_trades", []):
        key = str(trade.get("key") or "")
        side = str(trade.get("side") or "")
        exit_price = float(trade.get("exit_exec_price") or 0.0)
        notional = float(trade.get("notional_usdt") or 0.0)
        cur.execute(
            """INSERT OR REPLACE INTO paper_positions(
                paper_key, run_id, strategy, portfolio_id, mode, signal_id, symbol, side, status,
                lead_trader_name, detected_utc, last_seen_utc, entry_mark_price, entry_exec_price, exit_exec_price,
                notional_usdt, paper_pnl_usdt, payload_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                key,
                run_id,
                trade.get("strategy_name"),
                trade.get("portfolio_id"),
                trade.get("mode"),
                trade.get("signal_id"),
                trade.get("symbol"),
                side,
                "CLOSED",
                trade.get("lead_trader_name"),
                trade.get("detected_utc"),
                trade.get("last_seen_utc"),
                float(trade.get("entry_mark_price") or 0.0),
                float(trade.get("entry_exec_price") or 0.0),
                exit_price,
                notional,
                float(trade.get("paper_pnl_usdt") or 0.0),
                json.dumps(trade, ensure_ascii=False, sort_keys=True),
            ),
        )
        client_order_id = stable_id("binance_copy", key, "exit")
        cur.execute(
            """INSERT OR REPLACE INTO orders(
                order_id, ts_utc, bar_time_utc, mode, symbol, side, type, price, qty, status, reason, run_id, extra
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                client_order_id,
                trade.get("exit_detected_utc") or poll_utc,
                trade.get("exit_detected_utc") or poll_utc,
                "binance_copy_paper",
                MarkProvider.market_symbol(str(trade.get("symbol") or "")),
                _side_to_order_side(side, "exit"),
                "market",
                exit_price,
                _qty_from_notional(notional, exit_price),
                "shadow_closed",
                str(trade.get("exit_reason") or "paper_exit"),
                run_id,
                json.dumps({"sim": True, "paper_key": key, "never_submitted": True}, ensure_ascii=False, sort_keys=True),
            ),
        )

    eq = _equity_snapshot(state, float(cfg.get("initial_equity_usdt", 0.0) or 0.0))
    cur.execute(
        """INSERT OR REPLACE INTO equity(
            run_id, ts_utc, equity_usdt, cash_usdt, position_value_usdt, realized_pnl_cum, unrealized_pnl
        ) VALUES(?,?,?,?,?,?,?)""",
        (run_id, poll_utc, eq["equity"], eq["cash"], eq["position_value"], eq["realized_pnl_cum"], eq["unrealized_pnl"]),
    )
    con.commit()
    con.close()

    if args.shadow_orders_path:
        out = Path(args.shadow_orders_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in shadow_rows), encoding="utf-8")


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
    delegated_default = float(args.notional_usdt or cfg.get("paper_notional_usdt", 100.0))
    slippage_bp = float(args.slippage_bp if args.slippage_bp is not None else cfg.get("slippage_bp", 9.38))
    ttl_hours = float(args.ttl_hours or cfg.get("ttl_hours", 72.0))
    v21_config = str(args.v21_config or cfg.get("v21_config") or DEFAULT_V21_CONFIG)
    exit_on_reversal = bool(cfg.get("exit_on_reversal", True))
    if args.no_exit_on_reversal:
        exit_on_reversal = False

    for lead in cfg.get("leads", []):
        if not lead.get("enabled", True):
            continue
        enrich_existing_trades_with_lead_name(state, lead)
        portfolio_id = str(lead["portfolio_id"])
        mode = str(lead["mode"])
        delegated_capital = float(lead.get("delegated_capital_usdt") or delegated_default)
        v21_runtime = V21OneLegRuntime(v21_config, delegated_capital)
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
                v21_runtime=v21_runtime,
                now=now,
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
                v21_runtime=v21_runtime,
                now=now,
                slippage_bp=slippage_bp,
                ttl_hours=ttl_hours,
                exit_on_reversal=exit_on_reversal,
                trade_existing_history=args.trade_existing_history,
            )
        else:
            raise RuntimeError(f"unknown lead mode: {mode}")
        events.extend(lead_events)
        lead_meta.append({
            "name": lead["name"],
            "lead_trader_name": lead_trader_name(lead),
            "portfolio_id": portfolio_id,
            "mode": mode,
            "open_rows": len(open_positions),
            "history_rows": len(history),
            "events": len(lead_events),
            "v21_config": v21_config,
            "delegated_capital_usdt": delegated_capital,
            "base_order_pct_eq": 5.0,
        })

    state["last_poll"] = {"utc": iso(now), "paper_exchange": args.paper_exchange, "slippage_bp": slippage_bp, "ttl_hours": ttl_hours, "leads": lead_meta, "events": events}
    state["events"].extend({"utc": iso(now), **event} for event in events)
    state["events"] = state["events"][-args.max_events :]
    if not args.dry_run:
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    result = {
        "dry_run": args.dry_run,
        "state_path": str(state_path),
        "session_db": str(args.session_db) if args.session_db else "",
        "shadow_orders_path": str(args.shadow_orders_path) if args.shadow_orders_path else "",
        "open_paper_positions": len(state["open_positions"]),
        "closed_paper_trades": len(state["closed_trades"]),
        "events": events,
        "leads": lead_meta,
    }
    write_observability(
        state=state,
        result=result,
        cfg=cfg,
        args=args,
        run_id=str(getattr(args, "run_id", "") or "BINANCE_COPY_PAPER"),
        now=now,
    )
    return result


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Paper-live Binance online copytrading meta-strategy.")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--state-path", default=DEFAULT_STATE)
    ap.add_argument("--paper-exchange", default="bingx", choices=["bingx", "binance"])
    ap.add_argument("--notional-usdt", type=float, default=0.0)
    ap.add_argument("--v21-config", default=DEFAULT_V21_CONFIG)
    ap.add_argument("--slippage-bp", type=float, default=None)
    ap.add_argument("--ttl-hours", type=float, default=0.0)
    ap.add_argument("--history-page-size", type=int, default=50)
    ap.add_argument("--history-pages", type=int, default=2)
    ap.add_argument("--timeout-sec", type=float, default=20.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--session-db", default=DEFAULT_SESSION_DB)
    ap.add_argument("--shadow-orders-path", default=DEFAULT_SHADOW_ORDERS)
    ap.add_argument("--run-id", default="")
    ap.add_argument("--no-exit-on-reversal", action="store_true")
    ap.add_argument("--trade-existing-history", action="store_true", help="On first run, trade already visible history rows instead of seeding them as seen.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    ap.add_argument("--interval-sec", type=float, default=60.0)
    ap.add_argument("--max-events", type=int, default=2000)
    return ap


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = build_parser().parse_args()
    if not args.run_id:
        args.run_id = "BCP_" + utc_now().strftime("%Y%m%d_%H%M%S")
    while True:
        print(json.dumps(poll_once(args), ensure_ascii=False, indent=2))
        if not args.loop:
            break
        time.sleep(args.interval_sec)


if __name__ == "__main__":
    main()
