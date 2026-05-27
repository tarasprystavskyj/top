#!/usr/bin/env python3
"""BingX live HYPE cap100 canary.

This is a narrow live-order wrapper around hype_cap100_live_disabled_canary.
It keeps the same public Binance lead/market inputs and the same guard model,
but submits small BingX market orders when a guarded paper entry/exit would
otherwise be recorded.
"""
import argparse
import csv
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import hashlib
import json
import math
import os
import shlex
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import hype_cap100_live_disabled_canary as paper  # noqa: E402
from obw_platform.runners.common import CCXTFetcher  # noqa: E402
from obw_platform.runners.common import (  # noqa: E402
    db_upsert_open_position,
    ensure_orders_db,
    ensure_session_dbs,
    insert_order_row,
    make_bot_id,
)
try:
    from obw_platform.runners.live_runner_dual import (  # noqa: E402
        _extract_order_id,
        _fetch_exchange_position,
        _fetch_order_fill,
        _normalize_order_qty,
    )
except (ImportError, SyntaxError):  # pragma: no cover - exercised by import smoke on py3.6 hosts
    def _extract_order_id(order_obj: Optional[dict]) -> str:
        if not order_obj:
            return ""
        return str(order_obj.get("id") or order_obj.get("orderId") or order_obj.get("clientOrderId") or "")

    def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
        try:
            out = float(value)
        except Exception:
            return default
        return out if math.isfinite(out) else default

    def _decimal_step(value: float, step: float, rounding: str) -> float:
        if not step:
            return float(value)
        q = (Decimal(str(value)) / Decimal(str(step))).to_integral_value(rounding=rounding)
        return float(q * Decimal(str(step)))

    def _market_steps(fetcher: Any, symbol: str) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        ex = getattr(fetcher, "ex", fetcher)
        market = None
        try:
            if hasattr(ex, "load_markets"):
                ex.load_markets()
        except Exception:
            pass
        try:
            if hasattr(ex, "market"):
                market = ex.market(symbol)
        except Exception:
            market = None
        if not market:
            market = (getattr(ex, "markets", {}) or {}).get(symbol) or {}
        precision = market.get("precision") or {}
        limits = market.get("limits") or {}
        amount_precision = precision.get("amount")
        lot_step = None
        if isinstance(amount_precision, int):
            lot_step = 10.0 ** (-amount_precision)
        else:
            lot_step = _safe_float(amount_precision)
        min_qty = _safe_float((limits.get("amount") or {}).get("min"))
        return None, lot_step, min_qty

    def _normalize_order_qty(fetcher: Any, sym: str, qty: float, *, is_close: bool = False, max_qty: Optional[float] = None) -> float:
        _tick, lot_step, min_qty = _market_steps(fetcher, sym)
        q = float(qty or 0.0)
        if max_qty is not None:
            q = min(q, float(max_qty))
        if q <= 0:
            return 0.0
        if is_close:
            if lot_step:
                q = _decimal_step(q, lot_step, ROUND_DOWN)
            if max_qty is not None and q <= 0 and float(max_qty) > 0:
                q = float(max_qty)
            if min_qty and q < min_qty:
                q = min(float(min_qty), float(max_qty)) if max_qty is not None else float(min_qty)
            return max(0.0, q)
        if min_qty and q < min_qty:
            q = float(min_qty)
        if lot_step:
            q = _decimal_step(q, lot_step, ROUND_UP)
        return max(0.0, q)

    def _fetch_order_fill(fetcher: CCXTFetcher, sym: str, order_id: str, wait_sec: float = 3.0, poll_sec: float = 0.25):
        if not order_id:
            return None, None, None
        ccxt_sym = fetcher.resolve_symbol(sym) or sym
        deadline = time.time() + max(float(wait_sec), 0.0)
        last = None
        while True:
            try:
                od = fetcher.ex.fetch_order(order_id, ccxt_sym)
            except Exception:
                od = None
            if od:
                last = od
                status = str(od.get("status") or "").lower()
                avg = od.get("average") or od.get("price") or od.get("avgPrice") or od.get("avg_price")
                fill_dt = None
                ts = od.get("timestamp")
                if ts is not None:
                    fill_dt = datetime.fromtimestamp(int(ts) / 1000.0, tz=timezone.utc)
                elif od.get("datetime"):
                    try:
                        fill_dt = paper.parse_utc(str(od.get("datetime")))
                    except Exception:
                        fill_dt = None
                if status in {"closed", "filled"} and avg is not None:
                    return float(avg), fill_dt, od
                if status in {"canceled", "cancelled", "rejected", "expired"}:
                    return None, fill_dt, od
            if time.time() >= deadline:
                return None, None, last
            time.sleep(max(float(poll_sec), 0.05))

    def _normalize_position_side(raw: Any) -> str:
        text = str(raw or "").strip().upper()
        if text.startswith("LONG") or text in {"BUY", "BID"}:
            return "LONG"
        if text.startswith("SHORT") or text in {"SELL", "ASK"}:
            return "SHORT"
        return "BOTH" if text == "BOTH" else text

    def _extract_signed_position_qty(pos: Dict[str, Any]) -> float:
        info = pos.get("info", {}) if isinstance(pos.get("info"), dict) else {}
        for source in (pos, info):
            for key in ("contracts", "positionAmt", "positionAmount", "position", "size", "availableAmt", "holding"):
                value = _safe_float(source.get(key))
                if value is not None:
                    return float(value)
        return 0.0

    def _fetch_exchange_position(fetcher: CCXTFetcher, sym: str, side: str):
        ccxt_sym = fetcher.resolve_symbol(sym) or sym
        want_side = str(side).upper()
        try:
            positions = fetcher.ex.fetch_positions([ccxt_sym])
        except Exception:
            try:
                positions = fetcher.ex.fetch_positions()
            except Exception:
                return None
        rows = []
        has_negative_signed_qty = False
        for pos in positions or []:
            try:
                got_sym = fetcher.resolve_symbol(pos.get("symbol")) or pos.get("symbol")
                if got_sym != ccxt_sym:
                    continue
                signed_qty = _extract_signed_position_qty(pos)
                qty = abs(float(signed_qty or 0.0))
                if qty <= 0:
                    continue
                info = pos.get("info", {}) if isinstance(pos.get("info"), dict) else {}
                raw_side = ""
                for raw in (info.get("positionSide"), pos.get("positionSide"), info.get("posSide"), pos.get("posSide"), pos.get("side"), info.get("side")):
                    raw_side = _normalize_position_side(raw)
                    if raw_side in {"LONG", "SHORT", "BOTH"}:
                        break
                if signed_qty < 0:
                    has_negative_signed_qty = True
                entry = (
                    _safe_float(pos.get("entryPrice"))
                    or _safe_float(pos.get("entry"))
                    or _safe_float(info.get("avgPrice"))
                    or _safe_float(info.get("entryPrice"))
                    or 0.0
                )
                rows.append({"raw_side": raw_side, "signed_qty": signed_qty, "qty": qty, "entry": float(entry or 0.0)})
            except Exception:
                continue
        exact = []
        for row in rows:
            inferred = row["raw_side"] if row["raw_side"] in {"LONG", "SHORT"} else ""
            if not inferred and row["signed_qty"] < 0:
                inferred = "SHORT"
            elif not inferred and row["signed_qty"] > 0 and has_negative_signed_qty:
                inferred = "LONG"
            if inferred == want_side:
                exact.append({"symbol": ccxt_sym, "side": inferred, "qty": row["qty"], "entry": row["entry"]})
        if not exact:
            return None
        exact.sort(key=lambda row: row["qty"], reverse=True)
        return exact[0]


LIVE_REPORTS_ROOT = ROOT.parent / "top_1" / "obw_platform" / "_reports" / "_live"
DEFAULT_OUT_DIR = LIVE_REPORTS_ROOT / "hype_canary_bingx_live"
DEFAULT_ENV_FILE = "/var/www/vps2.happyuser.info/top/top_1/.env"
DEFAULT_LIVE_SYMBOL = "HYPE-USDT"
DEFAULT_COPY_POLL_INTERVAL_SEC = 1.0
DEFAULT_DCA_EVAL_INTERVAL_SEC = 60.0
DEFAULT_HISTORY_POLL_INTERVAL_SEC = 60.0


def stable_client_order_id(*parts: Any) -> str:
    text = "|".join(str(p or "") for p in parts)
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:18]
    return f"hypecap100-{digest}"


def order_id_from_response(order: Optional[Dict[str, Any]]) -> str:
    try:
        return _extract_order_id(order)
    except Exception:
        if not isinstance(order, dict):
            return ""
        return str(order.get("id") or order.get("orderId") or order.get("clientOrderId") or "")


def record_session_order(args: argparse.Namespace, *, now: datetime, symbol: str, side: str, type_: str, price: float, qty: float, status: str, reason: str, exchange_order_id: str = "", extra: Optional[Dict[str, Any]] = None) -> None:
    if not args.session_db:
        return
    insert_order_row(
        args.session_db,
        {
            "order_id": stable_client_order_id("session", type_, symbol, side, exchange_order_id, now.timestamp(), reason),
            "ts_utc": paper.iso(now),
            "bar_time_utc": paper.iso(now),
            "mode": "hype_cap100_bingx_live_canary",
            "symbol": symbol,
            "side": side,
            "type": type_,
            "price": float(price or 0.0),
            "qty": float(qty or 0.0),
            "status": status,
            "reason": reason,
            "run_id": args.run_id,
            "extra": json.dumps(extra or {}, ensure_ascii=False, sort_keys=True),
        },
    )


def upsert_session_position(args: argparse.Namespace, trade: Dict[str, Any], *, status: str, now: datetime, exchange_order_id: str = "", exit_fill: Optional[float] = None, close_reason: str = "") -> None:
    if not args.session_db:
        return
    bot_id = make_bot_id(args.out_dir, args.live_exchange, f"copy{args.interval_sec:g}_dca{args.dca_eval_interval_sec:g}")
    rec = {
        "symbol": args.live_symbol,
        "side": trade.get("side", "LONG"),
        "qty": float(trade.get("qty") or 0.0),
        "entry": float(trade.get("avg_entry") or trade.get("lead_entry_price") or 0.0),
        "tp_price": None,
        "sl_price": None,
        "ts_open": trade.get("opened_at_utc") or paper.iso(now),
        "run_id": args.run_id,
        "order_id": stable_client_order_id("local-position", trade.get("key"), trade.get("lead_position_id")),
        "exchange_order_id": exchange_order_id or trade.get("exchange_order_id"),
        "exchange": args.live_exchange,
        "timeframe": f"copy{args.interval_sec:g}_dca{args.dca_eval_interval_sec:g}",
        "status": status,
        "ts_close": paper.iso(now) if status != "OPEN" else None,
        "entry_fill": trade.get("avg_entry"),
        "entry_fill_ts": (trade.get("fills") or [{}])[-1].get("utc") if trade.get("fills") else None,
        "exit_fill": exit_fill,
        "exit_fill_ts": paper.iso(now) if exit_fill is not None else None,
        "close_reason": close_reason,
    }
    db_upsert_open_position(args.session_db, bot_id, rec)


def _fmt_float(value: Any) -> str:
    try:
        out = float(value or 0.0)
    except Exception:
        out = 0.0
    if not math.isfinite(out):
        out = 0.0
    return f"{out:.12g}"


def _ua_order_direction(order_type: Any, side: Any) -> str:
    action = "Закрити" if str(order_type or "").upper() in {"CLOSE", "EXIT"} else "Відкрити"
    side_text = str(side or "").upper()
    if side_text == "LONG":
        return f"{action} Long"
    if side_text == "SHORT":
        return f"{action} Short"
    return action


def _read_session_orders_for_artifacts(session_db: str) -> List[Dict[str, Any]]:
    if not session_db or not Path(session_db).exists():
        return []
    try:
        con = sqlite3.connect(f"file:{session_db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "orders" not in tables:
                return []
            rows = con.execute(
                """
                SELECT order_id, ts_utc, symbol, side, type, price, qty, status, reason, extra
                FROM orders
                WHERE UPPER(COALESCE(status, '')) = 'FILLED'
                ORDER BY ts_utc ASC, order_id ASC
                """
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            con.close()
    except Exception:
        return []


def export_match_ready_trade_history(args: argparse.Namespace) -> Optional[str]:
    rows = _read_session_orders_for_artifacts(str(args.session_db or ""))
    if not rows:
        return None
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    symbol_safe = str(args.live_symbol or DEFAULT_LIVE_SYMBOL).replace("-", "_")
    out_path = out_dir / f"{symbol_safe}_trade_history_for_match.csv"
    with out_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "Час виконання",
                "Ф’ючерси / Напрямок",
                "Виконано",
                "Ціна виконання",
                "Закриті PnL / %",
                "Комісія",
                "Ордер №",
                "Операція",
            ],
        )
        writer.writeheader()
        for row in rows:
            symbol = str(row.get("symbol") or args.live_symbol or DEFAULT_LIVE_SYMBOL).replace("-", "")
            writer.writerow(
                {
                    "Час виконання": row.get("ts_utc") or "",
                    "Ф’ючерси / Напрямок": f"{symbol}\n{_ua_order_direction(row.get('type'), row.get('side'))}",
                    "Виконано": _fmt_float(row.get("qty")),
                    "Ціна виконання": _fmt_float(row.get("price")),
                    "Закриті PnL / %": "0 USDT",
                    "Комісія": "0 USDT",
                    "Ордер №": row.get("order_id") or "",
                    "Операція": "",
                }
            )
    return str(out_path)


def write_live_equity_artifacts(args: argparse.Namespace, status: Dict[str, Any]) -> Optional[str]:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = str(status.get("utc") or paper.iso(paper.utc_now()))
    guards = status.get("guards") if isinstance(status.get("guards"), dict) else {}
    realized_plus_unrealized = float(guards.get("daily_realized_plus_unrealized_pnl_usdt") or 0.0)
    open_notional = float(guards.get("gross_open_notional") or 0.0)
    equity = float(getattr(args, "initial_equity", 0.0) or 0.0) + realized_plus_unrealized
    live_equity_path = out_dir / "live_equity.csv"
    existing: List[Dict[str, Any]] = []
    if live_equity_path.exists():
        try:
            with live_equity_path.open("r", newline="", encoding="utf-8-sig") as fh:
                existing = [row for row in csv.DictReader(fh) if row.get("ts") and row.get("ts") != ts]
        except Exception:
            existing = []
    existing.append(
        {
            "ts": ts,
            "value": _fmt_float(realized_plus_unrealized),
            "equity": _fmt_float(equity),
            "realized_plus_unrealized_pnl_usdt": _fmt_float(realized_plus_unrealized),
            "position_value_usdt": _fmt_float(open_notional),
        }
    )
    existing = existing[-10000:]
    with live_equity_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["ts", "value", "equity", "realized_plus_unrealized_pnl_usdt", "position_value_usdt"],
        )
        writer.writeheader()
        writer.writerows(existing)
    if args.session_db:
        try:
            con = sqlite3.connect(args.session_db)
            try:
                con.execute(
                    """
                    INSERT OR REPLACE INTO equity(
                        run_id, ts_utc, equity_usdt, cash_usdt, position_value_usdt,
                        realized_pnl_cum, unrealized_pnl
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        args.run_id,
                        ts,
                        equity,
                        equity - open_notional,
                        open_notional,
                        realized_plus_unrealized,
                        0.0,
                    ),
                )
                con.commit()
            finally:
                con.close()
        except Exception:
            pass
    return str(live_equity_path)


def emit_ui_artifacts(args: argparse.Namespace, status: Dict[str, Any]) -> Dict[str, Optional[str]]:
    return {
        "live_equity_csv": write_live_equity_artifacts(args, status),
        "match_ready_csv": export_match_ready_trade_history(args),
    }


def control_paths(args: argparse.Namespace) -> Tuple[Path, Path]:
    base = Path(args.control_dir or args.out_dir)
    return base / "STOP_NEW_ORDERS", base / "KILL"


def hot_stop_path(args: argparse.Namespace) -> Path:
    return Path(args.control_dir or args.out_dir) / "HOT_STOP"


def control_state(args: argparse.Namespace) -> Dict[str, Any]:
    stop_path, kill_path = control_paths(args)
    hot_path = hot_stop_path(args)
    return {
        "stop_new_orders_path": str(stop_path),
        "kill_path": str(kill_path),
        "hot_stop_path": str(hot_path),
        "stop_new_orders": stop_path.exists(),
        "kill": kill_path.exists(),
        "hot_stop": hot_path.exists(),
    }


def default_hot_restart_snapshot_path(args: argparse.Namespace) -> Path:
    return Path(args.hot_restart_snapshot_path or Path(args.out_dir) / "HOT_RESTART_SNAPSHOT.json")


def build_hot_restart_snapshot(
    args: argparse.Namespace,
    *,
    state: Dict[str, Any],
    status: Dict[str, Any],
    now: datetime,
    reason: str,
) -> Dict[str, Any]:
    return {
        "schema": "hype_cap100_live_hot_restart_snapshot_v1",
        "utc": paper.iso(now),
        "reason": reason,
        "pid": os.getpid(),
        "run_id": args.run_id,
        "paths": {
            "out_dir": str(args.out_dir),
            "state_path": str(args.state_path),
            "status_path": str(args.status_path),
            "telemetry_path": str(args.telemetry_path),
            "session_db": str(args.session_db or ""),
            "control_dir": str(args.control_dir or args.out_dir),
        },
        "runner_args": {
            "portfolio_id": args.portfolio_id,
            "symbol": args.symbol,
            "live_exchange": args.live_exchange,
            "live_symbol": args.live_symbol,
            "position_mode": args.position_mode,
            "initial_equity": args.initial_equity,
            "initial_target_notional": args.initial_target_notional,
            "max_gross_notional_usdt": args.max_gross_notional_usdt,
            "max_one_side_notional_usdt": args.max_one_side_notional_usdt,
            "max_daily_loss_usdt": args.max_daily_loss_usdt,
            "max_orders_per_hour": args.max_orders_per_hour,
            "deadline_utc": args.deadline_utc,
            "long_only": bool(args.long_only),
            "interval_sec": args.interval_sec,
            "dca_eval_interval_sec": args.dca_eval_interval_sec,
            "history_poll_interval_sec": args.history_poll_interval_sec,
            "order_sync_wait_sec": args.order_sync_wait_sec,
            "order_sync_poll_sec": args.order_sync_poll_sec,
        },
        "state": state,
        "status": status,
    }


def write_hot_restart_snapshot(
    args: argparse.Namespace,
    *,
    state: Dict[str, Any],
    status: Dict[str, Any],
    now: datetime,
    reason: str,
) -> str:
    snapshot = build_hot_restart_snapshot(args, state=state, status=status, now=now, reason=reason)
    path = default_hot_restart_snapshot_path(args)
    paper.write_json(path, snapshot)
    return str(path)


def load_resume_snapshot(args: argparse.Namespace) -> Optional[Dict[str, Any]]:
    if not args.resume_snapshot:
        return None
    snapshot_path = Path(args.resume_snapshot)
    snapshot = paper.load_json(snapshot_path, {})
    if snapshot.get("schema") != "hype_cap100_live_hot_restart_snapshot_v1":
        raise SystemExit(f"unsupported resume snapshot schema: {snapshot_path}")
    state = snapshot.get("state")
    if not isinstance(state, dict):
        raise SystemExit(f"resume snapshot has no state object: {snapshot_path}")
    existing_state_path = Path(args.state_path)
    if existing_state_path.exists() and not args.resume_snapshot_overwrite:
        raise SystemExit(f"state path already exists; use --resume-snapshot-overwrite to replace it: {existing_state_path}")
    state = dict(state)
    state["mode"] = "hype_cap100_bingx_live_canary"
    state["paper_only"] = False
    state.setdefault("events", []).append(
        {
            "utc": paper.iso(paper.utc_now()),
            "type": "resume_snapshot_loaded",
            "snapshot_path": str(snapshot_path),
            "snapshot_utc": snapshot.get("utc"),
        }
    )
    state["events"] = state["events"][-args.max_events :]
    paper.write_json(existing_state_path, state)
    return snapshot


def handle_hot_stop_if_requested(args: argparse.Namespace, controls: Dict[str, Any], now: datetime) -> None:
    if not controls.get("hot_stop"):
        return
    state = paper.load_json(Path(args.state_path), paper.default_state(args))
    status = paper.load_json(Path(args.status_path), {})
    if not status:
        status = status_payload(state, None, now, [], {"hot_stop": True, "inputs_skipped": True}, args)
    status["utc"] = paper.iso(now)
    status["hot_stop_requested"] = True
    status["control"] = control_state(args)
    snapshot_path = write_hot_restart_snapshot(args, state=state, status=status, now=now, reason="HOT_STOP")
    status["hot_restart_snapshot_path"] = snapshot_path
    paper.write_json(Path(args.status_path), status)
    paper.append_jsonl(Path(args.telemetry_path), {"event": "hot_stop", "utc": paper.iso(now), "snapshot_path": snapshot_path})
    raise SystemExit(f"HOT_STOP file present; snapshot written: {snapshot_path}")


def load_env_file(path: str) -> Dict[str, bool]:
    loaded: Dict[str, bool] = {}
    if not path:
        return loaded
    env_path = Path(path)
    if not env_path.exists():
        raise SystemExit(f"env file does not exist: {env_path}")
    for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        os.environ.setdefault(key, shlex.split(value.strip(), comments=False, posix=True)[0] if value.strip() else "")
        loaded[key] = bool(os.environ.get(key))
    return loaded


def safe_order(order: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(order, dict):
        return {}
    info = order.get("info") if isinstance(order.get("info"), dict) else {}
    safe = {
        "id": order.get("id") or order.get("orderId") or order.get("clientOrderId"),
        "clientOrderId": order.get("clientOrderId") or info.get("clientOrderId"),
        "symbol": order.get("symbol"),
        "type": order.get("type"),
        "side": order.get("side"),
        "amount": order.get("amount"),
        "filled": order.get("filled"),
        "remaining": order.get("remaining"),
        "average": order.get("average"),
        "price": order.get("price"),
        "status": order.get("status"),
        "timestamp": order.get("timestamp"),
        "datetime": order.get("datetime"),
        "reduceOnly": order.get("reduceOnly") or info.get("reduceOnly"),
        "positionSide": info.get("positionSide") or order.get("positionSide"),
    }
    return {k: v for k, v in safe.items() if v not in (None, "")}


def avg_price(order: Dict[str, Any], fallback: float) -> float:
    for key in ("average", "price"):
        try:
            value = float(order.get(key) or 0.0)
        except Exception:
            value = 0.0
        if math.isfinite(value) and value > 0:
            return value
    return float(fallback)


def live_client(args: argparse.Namespace) -> CCXTFetcher:
    client = getattr(args, "_live_client", None)
    if client is not None:
        return client
    loaded = load_env_file(args.env_file)
    client = CCXTFetcher(exchange=args.live_exchange, symbol_format="usdtm")
    args._live_client = client
    args._env_loaded_keys = sorted(k for k in loaded if any(t in k.upper() for t in ("KEY", "SECRET", "API")))
    return client


def auth_probe(args: argparse.Namespace) -> Dict[str, Any]:
    client = live_client(args)
    report = client.debug_credentials_report() if hasattr(client, "debug_credentials_report") else {}
    out = {
        "exchange": args.live_exchange,
        "credentials_present": bool(getattr(client, "credentials_present", False)),
        "key_found": bool((report or {}).get("key_found")),
        "secret_found": bool((report or {}).get("secret_found")),
        "fetch_balance_ok": False,
    }
    if not out["credentials_present"]:
        raise SystemExit("live credentials are not present after loading env file")
    bal = client.ex.fetch_balance()
    out["fetch_balance_ok"] = True
    out["balance_keys"] = sorted(list((bal or {}).keys()))[:20] if isinstance(bal, dict) else []
    return out


def live_order_params(client_order_id: str, side: str, reduce_only: bool) -> Dict[str, Any]:
    params: Dict[str, Any] = {"clientOrderId": client_order_id}
    if reduce_only:
        params["reduceOnly"] = True
    if side:
        params["positionSide"] = side.upper()
    return params


def submit_open(args: argparse.Namespace, symbol: str, side: str, expected_price: float, notional: float, client_order_id: str) -> Dict[str, Any]:
    client = live_client(args)
    ccxt_symbol = client.resolve_symbol(symbol) or client.resolve_symbol(args.live_symbol) or args.live_symbol
    qty = float(notional) / max(float(expected_price), 1e-12)
    qty = _normalize_order_qty(client, ccxt_symbol, qty, is_close=False)
    if qty <= 0:
        return {"ok": False, "error": "open_qty_zero_after_normalize", "qty": qty}
    params = live_order_params(client_order_id, side, reduce_only=False)
    order_side = "buy" if side.upper() == "LONG" else "sell"
    try:
        od = client.ex.create_order(ccxt_symbol, "market", order_side, qty, None, params)
    except Exception as exc:
        msg = str(exc).lower()
        if ("one-way mode" not in msg) and ("positionside" not in msg) and ("position side" not in msg):
            return {"ok": False, "error": str(exc), "qty": qty, "ccxt_symbol": ccxt_symbol}
        retry_params = {"clientOrderId": client_order_id}
        try:
            od = client.ex.create_order(ccxt_symbol, "market", order_side, qty, None, retry_params)
            params = retry_params
        except Exception as retry_exc:
            return {"ok": False, "error": str(retry_exc), "qty": qty, "ccxt_symbol": ccxt_symbol}
    ex_order_id = order_id_from_response(od)
    fill_px, fill_dt, fetched_order = _fetch_order_fill(client, ccxt_symbol, ex_order_id, wait_sec=args.order_sync_wait_sec, poll_sec=args.order_sync_poll_sec)
    if fill_px is None:
        return {"ok": False, "error": "open_timeout_no_fill", "qty": qty, "ccxt_symbol": ccxt_symbol, "order": fetched_order or od, "exchange_order_id": ex_order_id}
    ex_pos = _fetch_exchange_position(client, ccxt_symbol, side)
    return {
        "ok": True,
        "order": fetched_order or od,
        "qty": float((ex_pos or {}).get("qty") or qty),
        "entry": float((ex_pos or {}).get("entry") or fill_px),
        "fill_price": float(fill_px),
        "fill_dt": fill_dt.isoformat() if fill_dt else None,
        "params": params,
        "ccxt_symbol": ccxt_symbol,
        "exchange_order_id": ex_order_id,
        "exchange_position": ex_pos,
    }


def submit_close(args: argparse.Namespace, symbol: str, side: str, qty: float, client_order_id: str) -> Dict[str, Any]:
    client = live_client(args)
    ccxt_symbol = client.resolve_symbol(symbol) or client.resolve_symbol(args.live_symbol) or args.live_symbol
    ex_before = _fetch_exchange_position(client, ccxt_symbol, side)
    if not ex_before or float(ex_before.get("qty") or 0.0) <= 1e-12:
        return {"ok": True, "synced_only": True, "qty": 0.0, "ccxt_symbol": ccxt_symbol, "reason": "exchange_no_position_before_close"}
    qty = min(float(qty or 0.0), float(ex_before.get("qty") or 0.0))
    qty = _normalize_order_qty(client, ccxt_symbol, qty, is_close=True)
    if qty <= 0:
        return {"ok": False, "error": "close_qty_zero_after_normalize", "qty": qty}
    close_side = "sell" if side.upper() == "LONG" else "buy"
    params = live_order_params(client_order_id, side, reduce_only=True)
    try:
        od = client.ex.create_order(ccxt_symbol, "market", close_side, qty, None, params)
    except Exception as exc:
        msg = str(exc).lower()
        if ("no position to close" in msg) or ('code":101205' in msg) or ("code': 101205" in msg):
            return {"ok": True, "synced_only": True, "qty": 0.0, "ccxt_symbol": ccxt_symbol, "reason": "exchange_no_position_on_reduce"}
        if ("one-way mode" not in msg) and ("positionside" not in msg) and ("position side" not in msg) and ("reduceonly" not in msg) and ("reduce only" not in msg):
            return {"ok": False, "error": str(exc), "qty": qty, "ccxt_symbol": ccxt_symbol}
        retry_params: Dict[str, Any] = {"clientOrderId": client_order_id, "positionSide": side.upper()}
        if "reduceonly" not in msg and "reduce only" not in msg:
            retry_params["reduceOnly"] = True
        try:
            od = client.ex.create_order(ccxt_symbol, "market", close_side, qty, None, retry_params)
            params = retry_params
        except Exception as retry_exc:
            retry_msg = str(retry_exc).lower()
            if ("no position to close" in retry_msg) or ('code":101205' in retry_msg) or ("code': 101205" in retry_msg):
                return {"ok": True, "synced_only": True, "qty": 0.0, "ccxt_symbol": ccxt_symbol, "reason": "exchange_no_position_on_reduce_retry"}
            return {"ok": False, "error": str(retry_exc), "qty": qty, "ccxt_symbol": ccxt_symbol}
    ex_order_id = order_id_from_response(od)
    fill_px, fill_dt, fetched_order = _fetch_order_fill(client, ccxt_symbol, ex_order_id, wait_sec=args.order_sync_wait_sec, poll_sec=args.order_sync_poll_sec)
    if fill_px is None:
        return {"ok": False, "error": "close_timeout_no_fill", "qty": qty, "ccxt_symbol": ccxt_symbol, "order": fetched_order or od, "exchange_order_id": ex_order_id}
    ex_after = _fetch_exchange_position(client, ccxt_symbol, side)
    return {
        "ok": True,
        "order": fetched_order or od,
        "qty": qty,
        "fill_price": float(fill_px),
        "fill_dt": fill_dt.isoformat() if fill_dt else None,
        "params": params,
        "ccxt_symbol": ccxt_symbol,
        "exchange_order_id": ex_order_id,
        "exchange_position_before": ex_before,
        "exchange_position_after": ex_after,
    }


def dca_eval_due(state: Dict[str, Any], now: datetime, args: argparse.Namespace) -> Tuple[bool, Dict[str, Any]]:
    interval = float(getattr(args, "dca_eval_interval_sec", DEFAULT_DCA_EVAL_INTERVAL_SEC) or 0.0)
    if interval <= 0:
        return True, {"dca_eval_interval_sec": interval, "reason": "always"}
    epoch = float(now.timestamp())
    bucket = int(epoch // interval)
    phase = epoch - (bucket * interval)
    poll_window = max(float(getattr(args, "interval_sec", DEFAULT_COPY_POLL_INTERVAL_SEC) or DEFAULT_COPY_POLL_INTERVAL_SEC), 1.0)
    last_bucket = state.get("last_dca_eval_bucket")
    due = phase <= poll_window + 0.5 and last_bucket != bucket
    return due, {
        "dca_eval_interval_sec": interval,
        "dca_eval_bucket": bucket,
        "dca_eval_phase_sec": phase,
        "last_dca_eval_bucket": last_bucket,
        "due": due,
    }


def load_inputs_live(args: argparse.Namespace, state: Dict[str, Any], now: datetime) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[float], Dict[str, Any]]:
    if args.mock_open_long or args.mock_open_short or args.mock_no_position:
        return paper.mock_positions(args)
    session = requests.Session()
    try:
        positions, positions_meta = paper.fetch_open_positions(session, args.portfolio_id, args.timeout_sec)
        state["cached_positions"] = positions
        state["last_positions_poll_utc"] = paper.iso(now)
    except Exception as exc:
        positions = list(state.get("cached_positions") or [])
        positions_meta = {
            "error": str(exc),
            "cached_rows": len(positions),
            "last_positions_poll_utc": state.get("last_positions_poll_utc"),
        }
    try:
        mark, mark_meta = paper.fetch_mark(session, args.symbol, args.timeout_sec)
        state["cached_mark"] = mark
        state["last_mark_poll_utc"] = paper.iso(now)
    except Exception as exc:
        mark = state.get("cached_mark")
        mark_meta = {"error": str(exc), "cached_mark": mark, "last_mark_poll_utc": state.get("last_mark_poll_utc")}

    history_interval = float(getattr(args, "history_poll_interval_sec", DEFAULT_HISTORY_POLL_INTERVAL_SEC) or 0.0)
    last_history_ts = float(state.get("last_history_poll_ts") or 0.0)
    history_due = history_interval <= 0 or not state.get("cached_history") or (now.timestamp() - last_history_ts) >= history_interval
    if history_due:
        try:
            history, history_meta = paper.fetch_position_history(session, args.portfolio_id, args.timeout_sec, page_size=args.history_page_size)
            state["cached_history"] = history
            state["last_history_poll_ts"] = now.timestamp()
            state["last_history_poll_utc"] = paper.iso(now)
        except Exception as exc:
            history = list(state.get("cached_history") or [])
            history_meta = {
                "error": str(exc),
                "cached_rows": len(history),
                "last_history_poll_utc": state.get("last_history_poll_utc"),
                "history_poll_interval_sec": history_interval,
            }
    else:
        history = list(state.get("cached_history") or [])
        history_meta = {
            "skipped": True,
            "cached_rows": len(history),
            "last_history_poll_utc": state.get("last_history_poll_utc"),
            "history_poll_interval_sec": history_interval,
        }
    return positions, history, mark, {"positions": positions_meta, "history": history_meta, "market": mark_meta}


def sleep_until_next_poll(interval_sec: float) -> None:
    interval = max(float(interval_sec or DEFAULT_COPY_POLL_INTERVAL_SEC), 1.0)
    now = time.time()
    delay = interval - (now % interval)
    if delay < 0.05:
        delay += interval
    time.sleep(delay)


def sync_trade_from_exchange(args: argparse.Namespace, trade: Dict[str, Any]) -> Dict[str, Any]:
    client = live_client(args)
    ccxt_symbol = client.resolve_symbol(trade.get("symbol") or "") or client.resolve_symbol(args.live_symbol) or args.live_symbol
    ex_pos = _fetch_exchange_position(client, ccxt_symbol, str(trade.get("side") or "LONG"))
    if not ex_pos:
        return {"synced": False, "reason": "exchange_position_missing", "ccxt_symbol": ccxt_symbol}
    qty = float(ex_pos.get("qty") or 0.0)
    entry = float(ex_pos.get("entry") or 0.0)
    if qty > 0 and entry > 0:
        trade["qty"] = qty
        trade["avg_entry"] = entry
        trade["notional"] = qty * entry
        trade["exchange_position"] = ex_pos
        return {"synced": True, "qty": qty, "entry": entry, "notional": trade["notional"], "ccxt_symbol": ccxt_symbol}
    return {"synced": False, "reason": "exchange_position_zero", "ccxt_symbol": ccxt_symbol}


def live_add_fill(
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
    controls = control_state(args)
    if controls["stop_new_orders"]:
        return {
            "type": "live_entry_blocked",
            "key": trade.get("key"),
            "fill_type": fill_type,
            "reason": "stop_new_orders_file",
            "control": controls,
            "requested_notional": notional,
        }
    ok, guard_reason, guard_detail = paper.guard_new_entry(
        state, side=str(trade["side"]), add_notional=notional, mark=mark, now=now, args=args
    )
    if not ok:
        return {
            "type": "live_entry_blocked",
            "key": trade.get("key"),
            "fill_type": fill_type,
            "reason": guard_reason,
            "guard": guard_detail,
            "requested_notional": notional,
        }
    client_order_id = stable_client_order_id(
        "entry",
        trade.get("key"),
        trade.get("lead_position_id"),
        fill_type,
        trade.get("next_level_idx"),
    )
    submitted = submit_open(args, trade["symbol"], str(trade["side"]), expected_price, notional, client_order_id)
    if not submitted.get("ok"):
        record_session_order(
            args,
            now=now,
            symbol=args.live_symbol,
            side=str(trade["side"]),
            type_="OPEN",
            price=expected_price,
            qty=0.0,
            status="REJECTED",
            reason=str(submitted.get("error") or "unknown_order_error"),
            exchange_order_id=str(submitted.get("exchange_order_id") or ""),
            extra=submitted,
        )
        return {
            "type": "live_entry_failed",
            "key": trade.get("key"),
            "fill_type": fill_type,
            "reason": reason,
            "error": str(submitted.get("error") or "unknown_order_error"),
            "guard": guard_detail,
            "requested_notional": notional,
        }
    order = safe_order(submitted.get("order"))
    fill_price = float(submitted.get("entry") or submitted.get("fill_price") or avg_price(order, expected_price))
    qty = float(submitted.get("qty") or order.get("filled") or order.get("amount") or 0.0)
    live_notional = qty * fill_price
    trade["qty"] = float(trade.get("qty") or 0.0) + qty
    trade["notional"] = float(trade.get("notional") or 0.0) + live_notional
    trade["fees_paid"] = float(trade.get("fees_paid") or 0.0)
    trade["avg_entry"] = trade["notional"] / max(trade["qty"], 1e-12)
    fill = {
        "ts_ms": int(now.timestamp() * 1000),
        "utc": paper.iso(now),
        "risk_action": "new_entry",
        "symbol": trade["symbol"],
        "live_symbol": submitted.get("ccxt_symbol"),
        "side": trade["side"],
        "fill_type": fill_type,
        "reason": reason,
        "expected_price": expected_price,
        "live_fill_price": fill_price,
        "requested_notional": notional,
        "live_notional": live_notional,
        "qty": qty,
        "paper_only": False,
        "live_order": order,
        "client_order_id": client_order_id,
        "exchange_order_id": submitted.get("exchange_order_id"),
        "fill_dt": submitted.get("fill_dt"),
        "exchange_position": submitted.get("exchange_position"),
    }
    state.setdefault("paper_orders", []).append(fill)
    state["paper_orders"] = state["paper_orders"][-5000:]
    trade.setdefault("fills", []).append(fill)
    trade["exchange_order_id"] = submitted.get("exchange_order_id")
    upsert_session_position(args, trade, status="OPEN", now=now, exchange_order_id=str(submitted.get("exchange_order_id") or ""))
    record_session_order(
        args,
        now=now,
        symbol=args.live_symbol,
        side=str(trade["side"]),
        type_="OPEN",
        price=fill_price,
        qty=qty,
        status="FILLED",
        reason=reason,
        exchange_order_id=str(submitted.get("exchange_order_id") or ""),
        extra={"fill": fill, "submitted": submitted},
    )
    return {"type": "live_fill", "key": trade.get("key"), "fill": fill, "guard": guard_detail}


def live_close_trade(
    state: Dict[str, Any],
    trade: Dict[str, Any],
    *,
    now: datetime,
    expected_exit: float,
    mark: Optional[float],
    reason: str,
    history_row: Optional[Dict[str, Any]],
    args: argparse.Namespace,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    client_order_id = stable_client_order_id("exit", trade.get("key"), trade.get("lead_position_id"))
    submitted = submit_close(args, trade["symbol"], str(trade["side"]), float(trade.get("qty") or 0.0), client_order_id)
    if submitted.get("synced_only"):
        closed = paper.close_trade(trade, now=now, expected_exit=expected_exit, mark=mark, reason=str(submitted.get("reason") or reason), history_row=history_row)
        closed["paper_only"] = False
        closed["live_exit_synced_only"] = True
        closed["client_order_id"] = client_order_id
        upsert_session_position(args, closed, status="CLOSED", now=now, exit_fill=expected_exit, close_reason=str(submitted.get("reason") or reason))
        record_session_order(
            args,
            now=now,
            symbol=args.live_symbol,
            side=str(trade["side"]),
            type_="CLOSE",
            price=expected_exit,
            qty=0.0,
            status="SYNCED",
            reason=str(submitted.get("reason") or reason),
            extra=submitted,
        )
        return closed, {"type": "live_exit_synced", "key": trade.get("key"), "pnl": closed["paper_pnl_usdt"], "reason": submitted.get("reason") or reason}
    if not submitted.get("ok"):
        record_session_order(
            args,
            now=now,
            symbol=args.live_symbol,
            side=str(trade["side"]),
            type_="CLOSE",
            price=expected_exit,
            qty=float(trade.get("qty") or 0.0),
            status="REJECTED",
            reason=str(submitted.get("error") or "unknown_order_error"),
            exchange_order_id=str(submitted.get("exchange_order_id") or ""),
            extra=submitted,
        )
        return None, {
            "type": "live_exit_failed",
            "key": trade.get("key"),
            "reason": reason,
            "error": str(submitted.get("error") or "unknown_order_error"),
        }
    order = safe_order(submitted.get("order"))
    exit_price = float(submitted.get("fill_price") or avg_price(order, expected_exit))
    closed = paper.close_trade(trade, now=now, expected_exit=exit_price, mark=mark, reason=reason, history_row=history_row)
    closed["paper_only"] = False
    closed["live_exit_order"] = order
    closed["live_exit_price"] = exit_price
    closed["client_order_id"] = client_order_id
    closed["exchange_order_id"] = submitted.get("exchange_order_id")
    upsert_session_position(args, closed, status="CLOSED", now=now, exchange_order_id=str(submitted.get("exchange_order_id") or ""), exit_fill=exit_price, close_reason=reason)
    record_session_order(
        args,
        now=now,
        symbol=args.live_symbol,
        side=str(trade["side"]),
        type_="CLOSE",
        price=exit_price,
        qty=float(submitted.get("qty") or 0.0),
        status="FILLED",
        reason=reason,
        exchange_order_id=str(submitted.get("exchange_order_id") or ""),
        extra={"closed": closed, "submitted": submitted},
    )
    return closed, {"type": "live_exit", "key": trade.get("key"), "pnl": closed["paper_pnl_usdt"], "reason": reason, "live_order": order}


def apply_live_snapshot(
    state: Dict[str, Any],
    positions: List[Dict[str, Any]],
    history: List[Dict[str, Any]],
    mark: Optional[float],
    now: datetime,
    args: argparse.Namespace,
    allow_dca: bool,
) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    filtered = []
    for pos in positions:
        if pos.get("symbol") != args.symbol:
            continue
        if str(pos.get("side")) == "SHORT" and args.long_only:
            events.append({"type": "signal_ignored", "reason": "long_only", "key": pos.get("key")})
            continue
        filtered.append(pos)
    current = {f"{p['symbol']}:{p['side']}": p for p in filtered}
    open_trades: Dict[str, Dict[str, Any]] = state.setdefault("open_trades", {})

    for key, pos in sorted(current.items()):
        side = str(pos["side"])
        entry = float(pos["entry_price"])
        if key not in open_trades:
            plan = paper.build_plan(float(state.get("equity") or args.initial_equity), args, entry)
            trade = {
                "key": key,
                "lead_position_id": pos.get("id"),
                "symbol": pos["symbol"],
                "side": side,
                "opened_at_utc": paper.iso(now),
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
                "last_seen_utc": paper.iso(now),
            }
            event = live_add_fill(
                state,
                trade,
                now=now,
                expected_price=entry,
                notional=float(plan["base_notional"]),
                fill_type="base_entry",
                reason="lead_open_position_detected",
                mark=mark,
                args=args,
            )
            if event["type"] == "live_fill":
                open_trades[key] = trade
            events.append(event)
        else:
            trade = open_trades[key]
            trade["last_seen_utc"] = paper.iso(now)
            trade["last_mark"] = mark
            sync_meta = sync_trade_from_exchange(args, trade)
            if sync_meta.get("synced"):
                events.append({"type": "exchange_position_synced", "key": key, **sync_meta})

        if key not in open_trades:
            continue
        trade = open_trades[key]
        while allow_dca and int(trade.get("next_level_idx") or 0) < len(trade.get("levels") or []):
            idx = int(trade.get("next_level_idx") or 0)
            level = float(trade["levels"][idx])
            if mark is None or mark > level:
                break
            notional = float(trade["add_notionals"][idx])
            event = live_add_fill(
                state,
                trade,
                now=now,
                expected_price=level,
                notional=notional,
                fill_type=f"dca_add_{idx + 1}",
                reason="mark_crossed_dca_level",
                mark=mark,
                args=args,
            )
            events.append(event)
            if event["type"] != "live_fill":
                break
            trade["next_level_idx"] = idx + 1

    keys_to_close = set(open_trades) - set(current)
    for key in sorted(keys_to_close):
        trade = open_trades[key]
        hist = paper.find_history_exit(trade, history)
        if hist and hist.get("avg_close_price"):
            exit_price = float(hist["avg_close_price"])
            reason = "position_history_closed"
        else:
            exit_price = float(mark or trade.get("last_mark") or trade["lead_entry_price"])
            reason = "lead_position_disappeared_mark_fallback"
        closed, event = live_close_trade(state, trade, now=now, expected_exit=exit_price, mark=mark, reason=reason, history_row=hist, args=args)
        events.append(event)
        if closed is None:
            continue
        state["equity"] = float(state.get("equity") or args.initial_equity) + float(closed["paper_pnl_usdt"])
        state.setdefault("closed_trades", []).append(closed)
        del open_trades[key]
    return events


def status_payload(state: Dict[str, Any], mark: Optional[float], now: datetime, events: List[Dict[str, Any]], input_meta: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    payload = paper.status_payload(state, mark, now, events, input_meta, args)
    payload["paper_only"] = False
    payload["live_order_code_present"] = True
    payload["live_exchange"] = args.live_exchange
    payload["live_symbol"] = args.live_symbol
    payload["position_mode"] = args.position_mode
    payload["auth_probe"] = getattr(args, "_auth_probe", {})
    payload["copy_poll_interval_sec"] = args.interval_sec
    payload["dca_eval_interval_sec"] = args.dca_eval_interval_sec
    payload["history_poll_interval_sec"] = args.history_poll_interval_sec
    payload["dca_eval_meta"] = getattr(args, "_last_dca_eval_meta", {})
    payload["control"] = control_state(args)
    payload["session_db"] = args.session_db
    payload["run_id"] = args.run_id
    payload["order_sync_wait_sec"] = args.order_sync_wait_sec
    return payload


def poll_once(args: argparse.Namespace) -> Dict[str, Any]:
    now = paper.utc_now()
    controls = control_state(args)
    handle_hot_stop_if_requested(args, controls, now)
    if controls["kill"]:
        raise SystemExit(f"KILL file present: {controls['kill_path']}")
    state = paper.load_json(Path(args.state_path), paper.default_state(args))
    state["mode"] = "hype_cap100_bingx_live_canary"
    state["paper_only"] = False
    positions, history, mark, input_meta = load_inputs_live(args, state, now)
    allow_dca, dca_meta = dca_eval_due(state, now, args)
    args._last_dca_eval_meta = dca_meta
    events = apply_live_snapshot(state, positions, history, mark, now, args, allow_dca=allow_dca)
    if allow_dca:
        state["last_dca_eval_bucket"] = dca_meta.get("dca_eval_bucket")
        state["last_dca_eval_utc"] = paper.iso(now)
    status = status_payload(state, mark, now, events, input_meta, args)
    status["ui_artifacts"] = emit_ui_artifacts(args, status)
    state["last_poll"] = status
    state.setdefault("events", []).extend({"utc": paper.iso(now), **event} for event in events)
    state["events"] = state["events"][-args.max_events :]
    paper.write_json(Path(args.state_path), state)
    paper.write_json(Path(args.status_path), status)
    paper.append_jsonl(Path(args.telemetry_path), {"event": "poll", "status": status})
    for event in events:
        paper.append_jsonl(Path(args.telemetry_path), {"event": "live_event", "utc": paper.iso(now), **event})
    return status


def build_arg_parser() -> argparse.ArgumentParser:
    ap = paper.build_arg_parser()
    ap.description = "BingX live HYPE cap100 canary."
    ap.set_defaults(out_dir=str(DEFAULT_OUT_DIR), interval_sec=DEFAULT_COPY_POLL_INTERVAL_SEC)
    ap.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    ap.add_argument("--live-exchange", default="bingx")
    ap.add_argument("--live-symbol", default=DEFAULT_LIVE_SYMBOL)
    ap.add_argument("--position-mode", choices=["oneway", "hedge"], default="oneway")
    ap.add_argument("--dca-eval-interval-sec", type=float, default=DEFAULT_DCA_EVAL_INTERVAL_SEC)
    ap.add_argument("--history-poll-interval-sec", type=float, default=DEFAULT_HISTORY_POLL_INTERVAL_SEC)
    ap.add_argument("--control-dir", default="", help="Directory containing STOP_NEW_ORDERS and KILL files. Defaults to out-dir.")
    ap.add_argument("--session-db", default="")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--order-sync-wait-sec", type=float, default=3.0)
    ap.add_argument("--order-sync-poll-sec", type=float, default=0.25)
    ap.add_argument("--hot-restart-snapshot-path", default="", help="Where HOT_STOP writes a restart snapshot. Defaults to out-dir/HOT_RESTART_SNAPSHOT.json.")
    ap.add_argument("--resume-snapshot", default="", help="Load state from a HOT_STOP snapshot before starting.")
    ap.add_argument("--resume-snapshot-overwrite", action="store_true", help="Allow --resume-snapshot to replace an existing state-path.")
    return ap


def normalize_paths(args: argparse.Namespace) -> None:
    paper.normalize_paths(args)
    out_dir = Path(args.out_dir)
    if args.session_db and not Path(args.session_db).is_absolute() and Path(args.session_db).parent == Path("."):
        args.session_db = str(out_dir / args.session_db)


def validate_args(args: argparse.Namespace) -> None:
    paper.validate_args(args)
    if not args.live_symbol:
        raise SystemExit("--live-symbol is required")
    if args.dca_eval_interval_sec < 0:
        raise SystemExit("--dca-eval-interval-sec must be non-negative")
    if args.history_poll_interval_sec < 0:
        raise SystemExit("--history-poll-interval-sec must be non-negative")
    if args.order_sync_wait_sec < 0 or args.order_sync_poll_sec <= 0:
        raise SystemExit("order sync timing must be non-negative/positive")
    if args.resume_snapshot and not Path(args.resume_snapshot).exists():
        raise SystemExit(f"--resume-snapshot does not exist: {args.resume_snapshot}")


def main() -> None:
    args = build_arg_parser().parse_args()
    normalize_paths(args)
    validate_args(args)
    if not args.session_db:
        args.session_db = str(Path(args.out_dir) / "session.sqlite")
    if not args.run_id:
        args.run_id = "HYPE_CAP100_LIVE_" + paper.utc_now().strftime("%Y%m%dT%H%M%SZ")
    ensure_session_dbs(args.out_dir, args.session_db)
    ensure_orders_db(args.session_db)
    load_resume_snapshot(args)
    args._auth_probe = auth_probe(args)
    while True:
        print(json.dumps(poll_once(args), ensure_ascii=False, indent=2, sort_keys=True), flush=True)
        if not args.loop:
            break
        if paper.utc_now() >= paper.parse_utc(args.deadline_utc):
            break
        sleep_until_next_poll(args.interval_sec)


if __name__ == "__main__":
    main()
