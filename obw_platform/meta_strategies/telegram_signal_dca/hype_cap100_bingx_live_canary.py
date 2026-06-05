#!/usr/bin/env python3
"""Exchange live HYPE cap100 canary.

This is a narrow live-order wrapper around hype_cap100_live_disabled_canary.
It keeps the same public Binance lead/market inputs and the same guard model,
but submits small exchange market orders when a guarded paper entry/exit would
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

try:
    from . import hype_cap100_live_disabled_canary as paper  # noqa: E402
    from . import hype_copy_signal_meta_strategy as copy_signal_meta  # noqa: E402
except ImportError:  # pragma: no cover - script import path
    import hype_cap100_live_disabled_canary as paper  # noqa: E402
    import hype_copy_signal_meta_strategy as copy_signal_meta  # noqa: E402
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
DEFAULT_ENV_FILE = "/var/www/vps2.happyuser.info/top/top_1/obw_platform/.env"
BINGX_LEGACY_ENV_FILE = "/var/www/vps2.happyuser.info/top/top_1/.env"
DEFAULT_HTX_FRIEND_LIVE_CONFIG = ROOT / "meta_strategies" / "telegram_signal_dca" / "configs" / "htx_veronika_hype_live_110.json"
DEFAULT_LIVE_SYMBOL = "HYPE-USDT"
SUPPORTED_LIVE_EXCHANGES = ("bingx", "gateio", "htx", "mexc")
SUPPORTED_LIVE_EXCHANGE_PROFILES = ("gateio_current", "bingx_legacy", "htx_current", "mexc_current")
SUPPORTED_SOURCE_LEVERAGE_MODES = ("ignore", "copy", "copy_div2", "fixed")
LIVE_EXCHANGE_PROFILES = {
    "gateio_current": {
        "live_exchange": "gateio",
        "live_symbol": DEFAULT_LIVE_SYMBOL,
        "position_mode": "oneway",
        "env_file": DEFAULT_ENV_FILE,
    },
    "bingx_legacy": {
        "live_exchange": "bingx",
        "live_symbol": DEFAULT_LIVE_SYMBOL,
        "position_mode": "hedge",
        "env_file": BINGX_LEGACY_ENV_FILE,
    },
    "htx_current": {
        "live_exchange": "htx",
        "live_symbol": "HYPE/USDT:USDT",
        "position_mode": "oneway",
        "env_file": DEFAULT_ENV_FILE,
    },
    "mexc_current": {
        "live_exchange": "mexc",
        "live_symbol": "HYPE/USDT:USDT",
        "position_mode": "oneway",
        "env_file": DEFAULT_ENV_FILE,
    },
}
DEFAULT_COPY_POLL_INTERVAL_SEC = 1.0
DEFAULT_DCA_EVAL_INTERVAL_SEC = 60.0
DEFAULT_HISTORY_POLL_INTERVAL_SEC = 60.0
DEFAULT_ORDER_ERROR_BACKOFF_SEC = 300.0
DEFAULT_ORDER_ERROR_CIRCUIT_SEC = 1800.0
DEFAULT_ORDER_ERROR_MAX_CONSECUTIVE = 3
DEFAULT_ENTRY_FAILURE_COOLDOWN_SEC = 3600.0
DEFAULT_MAX_ORDER_ATTEMPTS_PER_HOUR = 20
DEFAULT_ORDER_POST_THROTTLE_SEC = 2.0
DEFAULT_MARK_POLL_INTERVAL_SEC = 0.0
DEFAULT_SOURCE_SIZE_SYNC_INTERVAL_SEC = 60.0
DEFAULT_SOURCE_SIZE_CLAMP_TELEMETRY_SEC = 300.0
SOURCE_BOX_GUARD_HEADROOM = 1.02


def stable_client_order_id(*parts: Any) -> str:
    text = "|".join(str(p or "") for p in parts)
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:18]
    return f"hypecap100-{digest}"


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))


def active_pointer_paths(args: argparse.Namespace) -> Dict[str, Path]:
    out_dir = Path(args.out_dir)
    return {
        "status_path": out_dir / "ACTIVE_STATUS_PATH.txt",
        "telemetry_path": out_dir / "ACTIVE_TELEMETRY_PATH.txt",
        "state_path": out_dir / "ACTIVE_STATE_PATH.txt",
        "session_db": out_dir / "ACTIVE_SESSION_DB_PATH.txt",
        "log_path": out_dir / "ACTIVE_LOG_PATH.txt",
    }


def active_log_path_arg(args: argparse.Namespace) -> str:
    return str(
        getattr(args, "stdout_log_path", "")
        or os.getenv("HYPE_CAP100_STDOUT_LOG_PATH", "")
        or os.getenv("LIVE_STDOUT_LOG_PATH", "")
        or ""
    )


def write_active_pointers(args: argparse.Namespace) -> Dict[str, str]:
    log_path = active_log_path_arg(args)
    if not log_path:
        log_path = str(Path(args.out_dir) / "__ACTIVE_LOG_PATH_NOT_PROVIDED__")
    values = {
        "status_path": str(args.status_path),
        "telemetry_path": str(args.telemetry_path),
        "state_path": str(args.state_path),
        "session_db": str(args.session_db or ""),
        "log_path": log_path,
    }
    for key, marker in active_pointer_paths(args).items():
        _atomic_write_text(marker, values.get(key, "") + "\n")
    return values


def active_pointer_sanity(args: argparse.Namespace, status: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": True, "pointers": {}}
    expected = {
        "status_path": str(args.status_path),
        "telemetry_path": str(args.telemetry_path),
        "state_path": str(args.state_path),
        "session_db": str(args.session_db or ""),
    }
    for key, marker in active_pointer_paths(args).items():
        try:
            value = marker.read_text(encoding="utf-8").strip()
        except Exception:
            value = ""
        out["pointers"][key] = value
        if key in expected and value != expected[key]:
            out["ok"] = False
            out.setdefault("mismatches", {})[key] = {"expected": expected[key], "actual": value}
    status_telemetry = str((status or {}).get("telemetry_path") or "")
    if status_telemetry and out["pointers"].get("telemetry_path") != status_telemetry:
        out["ok"] = False
        out.setdefault("mismatches", {})["status.telemetry_path"] = {
            "expected": status_telemetry,
            "actual": out["pointers"].get("telemetry_path"),
        }
    return out


def order_id_from_response(order: Optional[Dict[str, Any]]) -> str:
    try:
        return _extract_order_id(order)
    except Exception:
        if not isinstance(order, dict):
            return ""
        return str(order.get("id") or order.get("orderId") or order.get("clientOrderId") or "")


def gateio_client_order_text(client_order_id: str) -> str:
    text = str(client_order_id or "")
    if text.startswith("t-") and len(text) <= 28:
        return text
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:18]
    return f"t-hcap100-{digest}"


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


def ensure_order_execution_comparisons_db(db_path: str) -> None:
    if not db_path:
        return
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS order_execution_comparisons (
                comparison_id TEXT PRIMARY KEY,
                run_id TEXT,
                ts_utc TEXT,
                symbol TEXT,
                side TEXT,
                action TEXT,
                trade_key TEXT,
                lead_position_id TEXT,
                signal_time_utc TEXT,
                signal_price REAL,
                exchange_fill_time_utc TEXT,
                exchange_fill_price REAL,
                source_opened_utc TEXT,
                source_closed_utc TEXT,
                source_avg_cost REAL,
                source_avg_close REAL,
                lag_sec REAL,
                exchange_vs_signal_bp REAL,
                source_vs_signal_bp REAL,
                exchange_vs_source_bp REAL,
                source_history_valid INTEGER,
                source_history_reject_reason TEXT,
                exchange_order_id TEXT,
                client_order_id TEXT,
                extra_json TEXT
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_order_execution_comparisons_run ON order_execution_comparisons(run_id, ts_utc)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_order_execution_comparisons_trade ON order_execution_comparisons(trade_key, action)")
        con.commit()
    finally:
        con.close()


def _parse_iso_dt(raw: Any) -> Optional[datetime]:
    return copy_signal_meta.parse_iso_dt(raw)


def _ms_to_dt(raw: Any) -> Optional[datetime]:
    return copy_signal_meta.ms_to_dt(raw)


def _time_lag_sec(a: Optional[datetime], b: Optional[datetime]) -> Optional[float]:
    return copy_signal_meta.time_lag_sec(a, b)


def _bp_delta(ref_price: Any, test_price: Any, side: str = "LONG", *, is_close: bool = False) -> Optional[float]:
    return signed_slip_bp(side, ref_price, test_price, is_close=is_close)


def _source_history_open_dt(row: Dict[str, Any]) -> Optional[datetime]:
    return copy_signal_meta.source_history_open_dt(row)


def _source_history_close_dt(row: Dict[str, Any]) -> Optional[datetime]:
    return copy_signal_meta.source_history_close_dt(row)


def validate_source_history_match(trade: Dict[str, Any], row: Optional[Dict[str, Any]], close_time: datetime) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    return copy_signal_meta.validate_source_history_match(trade, row, close_time, iso_fn=paper.iso)


def find_valid_source_history_exit(trade: Dict[str, Any], history: List[Dict[str, Any]], close_time: datetime) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    return copy_signal_meta.find_valid_source_history_exit(trade, history, close_time, iso_fn=paper.iso)


def record_order_execution_comparison(
    args: argparse.Namespace,
    *,
    now: datetime,
    action: str,
    trade: Dict[str, Any],
    signal_price: Optional[float],
    exchange_fill_price: Optional[float],
    exchange_fill_time_utc: Optional[str],
    source_history: Optional[Dict[str, Any]] = None,
    source_meta: Optional[Dict[str, Any]] = None,
    exchange_order_id: str = "",
    client_order_id: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    if not args.session_db:
        return
    ensure_order_execution_comparisons_db(args.session_db)
    source_history = source_history if isinstance(source_history, dict) else {}
    source_meta = source_meta if isinstance(source_meta, dict) else {}
    signal_dt = _parse_iso_dt(trade.get("opened_at_utc")) if str(action).upper() == "OPEN" else now
    fill_dt = _parse_iso_dt(exchange_fill_time_utc) or now
    source_price = source_history.get("avg_close_price") if str(action).upper() == "CLOSE" else source_history.get("avg_cost")
    lag_sec = (fill_dt - signal_dt).total_seconds() if signal_dt and fill_dt else None
    side = str(trade.get("side") or "LONG")
    is_close = str(action).upper() == "CLOSE"
    row = {
        "comparison_id": stable_client_order_id("comparison", args.run_id, action, trade.get("key"), exchange_order_id, client_order_id, paper.iso(now)),
        "run_id": args.run_id,
        "ts_utc": paper.iso(now),
        "symbol": args.live_symbol,
        "side": side,
        "action": str(action).upper(),
        "trade_key": str(trade.get("key") or ""),
        "lead_position_id": str(trade.get("lead_position_id") or ""),
        "signal_time_utc": paper.iso(signal_dt) if signal_dt else "",
        "signal_price": float(signal_price) if signal_price not in (None, "") else None,
        "exchange_fill_time_utc": paper.iso(fill_dt) if fill_dt else "",
        "exchange_fill_price": float(exchange_fill_price) if exchange_fill_price not in (None, "") else None,
        "source_opened_utc": str(source_history.get("opened_utc") or ""),
        "source_closed_utc": str(source_history.get("closed_utc") or ""),
        "source_avg_cost": float(source_history.get("avg_cost")) if source_history.get("avg_cost") not in (None, "") else None,
        "source_avg_close": float(source_history.get("avg_close_price")) if source_history.get("avg_close_price") not in (None, "") else None,
        "lag_sec": lag_sec,
        "exchange_vs_signal_bp": _bp_delta(signal_price, exchange_fill_price, side, is_close=is_close),
        "source_vs_signal_bp": _bp_delta(signal_price, source_price, side, is_close=is_close),
        "exchange_vs_source_bp": _bp_delta(source_price, exchange_fill_price, side, is_close=is_close),
        "source_history_valid": 1 if source_meta.get("valid") else 0,
        "source_history_reject_reason": "" if source_meta.get("valid") else str(source_meta.get("reason") or ""),
        "exchange_order_id": str(exchange_order_id or ""),
        "client_order_id": str(client_order_id or ""),
        "extra_json": json.dumps({"source_meta": source_meta, **(extra or {})}, ensure_ascii=False, sort_keys=True),
    }
    con = sqlite3.connect(args.session_db)
    try:
        cols = list(row)
        con.execute(
            f"INSERT OR REPLACE INTO order_execution_comparisons({','.join(cols)}) VALUES({','.join(['?'] * len(cols))})",
            [row[c] for c in cols],
        )
        con.commit()
    finally:
        con.close()


def mark_stale_session_positions_closed(
    args: argparse.Namespace,
    *,
    symbol: str,
    side: str,
    now: datetime,
    close_reason: str,
    exit_fill: Optional[float] = None,
    exit_fill_ts: Optional[str] = None,
    exit_slip_bp: Optional[float] = None,
    exit_lag_sec: Optional[float] = None,
) -> int:
    if not args.session_db:
        return 0
    timeframe = f"copy{args.interval_sec:g}_dca{args.dca_eval_interval_sec:g}"
    try:
        con = sqlite3.connect(args.session_db)
        try:
            cur = con.cursor()
            cur.execute(
                """
                UPDATE open_positions
                SET status='CLOSED',
                    ts_close=?,
                    exit_fill=COALESCE(?, exit_fill),
                    exit_fill_ts=COALESCE(?, exit_fill_ts),
                    exit_slip_bp=COALESCE(?, exit_slip_bp),
                    exit_lag_sec=COALESCE(?, exit_lag_sec),
                    close_reason=?
                WHERE status='OPEN'
                  AND symbol=?
                  AND side=?
                  AND exchange=?
                  AND timeframe=?
                """,
                (
                    paper.iso(now),
                    exit_fill,
                    exit_fill_ts,
                    exit_slip_bp,
                    exit_lag_sec,
                    close_reason,
                    symbol,
                    side,
                    args.live_exchange,
                    timeframe,
                ),
            )
            changed = int(cur.rowcount or 0)
            con.commit()
            return changed
        finally:
            con.close()
    except Exception:
        return 0


def upsert_session_position(args: argparse.Namespace, trade: Dict[str, Any], *, status: str, now: datetime, exchange_order_id: str = "", exit_fill: Optional[float] = None, close_reason: str = "") -> None:
    if not args.session_db:
        return
    bot_id = make_bot_id(args.out_dir, args.live_exchange, f"copy{args.interval_sec:g}_dca{args.dca_eval_interval_sec:g}")
    fills = trade.get("fills") if isinstance(trade.get("fills"), list) else []
    last_fill = fills[-1] if fills else {}
    entry_slip_bp = last_fill.get("entry_slip_bp") if isinstance(last_fill, dict) else None
    entry_lag_sec = last_fill.get("entry_lag_sec") if isinstance(last_fill, dict) else None
    entry_mark_price = last_fill.get("mark") if isinstance(last_fill, dict) else None
    exit_slip_bp = trade.get("exit_slip_bp")
    exit_lag_sec = trade.get("exit_lag_sec")
    exit_fill_ts = trade.get("exit_fill_ts") or (paper.iso(now) if exit_fill is not None else None)
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
        "exit_fill_ts": exit_fill_ts,
        "close_reason": close_reason,
        "entry_slip_bp": entry_slip_bp,
        "entry_lag_sec": entry_lag_sec,
        "exit_slip_bp": exit_slip_bp,
        "exit_lag_sec": exit_lag_sec,
        "entry_mark_price": entry_mark_price,
        "exit_mark_price": trade.get("exit_mark_price"),
    }
    db_upsert_open_position(args.session_db, bot_id, rec)
    if status != "OPEN":
        mark_stale_session_positions_closed(
            args,
            symbol=str(trade.get("symbol") or args.live_symbol),
            side=str(trade.get("side", "LONG")),
            now=now,
            close_reason=close_reason or "session_position_closed",
            exit_fill=exit_fill,
            exit_fill_ts=exit_fill_ts,
            exit_slip_bp=exit_slip_bp,
            exit_lag_sec=exit_lag_sec,
        )


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


def _read_session_orders_for_artifacts(session_db: str, run_id: str = "", *, all_runs: bool = False) -> List[Dict[str, Any]]:
    if not session_db or not Path(session_db).exists():
        return []
    try:
        con = sqlite3.connect(f"file:{session_db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "orders" not in tables:
                return []
            has_run_id = "run_id" in {r[1] for r in con.execute("PRAGMA table_info(orders)").fetchall()}
            params: List[Any] = []
            where = "WHERE UPPER(COALESCE(status, '')) = 'FILLED'"
            if run_id and has_run_id and not all_runs:
                where += " AND COALESCE(run_id, '') = ?"
                params.append(run_id)
            rows = con.execute(
                f"""
                SELECT order_id, ts_utc, symbol, side, type, price, qty, status, reason, extra
                FROM orders
                {where}
                ORDER BY ts_utc ASC, order_id ASC
                """,
                params,
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            con.close()
    except Exception:
        return []


def safe_artifact_name(value: Any) -> str:
    text = str(value or DEFAULT_LIVE_SYMBOL).strip() or DEFAULT_LIVE_SYMBOL
    for ch in "\\/:*?\"<>|":
        text = text.replace(ch, "_")
    return text.replace("-", "_")


def export_match_ready_trade_history(args: argparse.Namespace) -> Optional[str]:
    rows = _read_session_orders_for_artifacts(str(args.session_db or ""), str(args.run_id or ""))
    if not rows:
        return None
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    symbol_safe = safe_artifact_name(args.live_symbol or DEFAULT_LIVE_SYMBOL)
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
            extra = _load_order_extra(row)
            pnl = _chart_event_pnl(extra) if str(row.get("type") or "").upper() in {"CLOSE", "EXIT"} else ""
            writer.writerow(
                {
                    "Час виконання": row.get("ts_utc") or "",
                    "Ф’ючерси / Напрямок": f"{symbol}\n{_ua_order_direction(row.get('type'), row.get('side'))}",
                    "Виконано": _fmt_float(row.get("qty")),
                    "Ціна виконання": _fmt_float(row.get("price")),
                    "Закриті PnL / %": f"{pnl} USDT" if pnl else "",
                    "Комісія": f"{_fmt_float(_order_fee_from_extra(extra))} USDT",
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


def _artifact_timeframe(args: argparse.Namespace) -> str:
    return f"copy{float(args.interval_sec):g}s_dca{float(args.dca_eval_interval_sec):g}s"


def write_live_candles_artifact(args: argparse.Namespace, status: Dict[str, Any]) -> Optional[str]:
    input_meta = status.get("input_meta") if isinstance(status.get("input_meta"), dict) else {}
    market = input_meta.get("market") if isinstance(input_meta.get("market"), dict) else {}
    mark = market.get("mark") or market.get("cached_mark")
    try:
        price = float(mark or 0.0)
    except Exception:
        price = 0.0
    if not math.isfinite(price) or price <= 0:
        return None
    ts = str(status.get("utc") or paper.iso(paper.utc_now()))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "live_candles.csv"
    existing: List[Dict[str, Any]] = []
    if path.exists():
        try:
            with path.open("r", newline="", encoding="utf-8-sig") as fh:
                existing = [row for row in csv.DictReader(fh) if row.get("ts") and row.get("ts") != ts]
        except Exception:
            existing = []
    existing.append(
        {
            "ts": ts,
            "open": _fmt_float(price),
            "high": _fmt_float(price),
            "low": _fmt_float(price),
            "close": _fmt_float(price),
            "volume": "0",
            "symbol": str(args.live_symbol or DEFAULT_LIVE_SYMBOL),
            "timeframe": _artifact_timeframe(args),
            "source": "binance_public_mark_snapshot",
        }
    )
    existing = existing[-10000:]
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["ts", "open", "high", "low", "close", "volume", "symbol", "timeframe", "source"],
        )
        writer.writeheader()
        writer.writerows(existing)
    return str(path)


def write_live_cache_npz_artifact(args: argparse.Namespace, candles_csv_path: Optional[str]) -> Optional[str]:
    if not candles_csv_path:
        return None
    try:
        import numpy as np
    except Exception:
        return None
    candles_path = Path(candles_csv_path)
    if not candles_path.exists():
        return None
    rows: List[Dict[str, Any]] = []
    try:
        with candles_path.open("r", newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                ts_raw = row.get("ts")
                if not ts_raw:
                    continue
                try:
                    ts = paper.parse_utc(str(ts_raw))
                    close = float(row.get("close") or 0.0)
                    if not math.isfinite(close) or close <= 0:
                        continue
                    rows.append(
                        {
                            "timestamp_s": int(ts.timestamp()),
                            "open": float(row.get("open") or close),
                            "high": float(row.get("high") or close),
                            "low": float(row.get("low") or close),
                            "close": close,
                            "volume": float(row.get("volume") or 0.0),
                        }
                    )
                except Exception:
                    continue
    except Exception:
        return None
    if not rows:
        return None
    rows = rows[-int(getattr(args, "live_cache_npz_max_bars", 10000) or 10000) :]
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    npz_path = Path(getattr(args, "live_cache_npz_path", "") or out_dir / "live_mark_ohlcv.npz")
    if not npz_path.is_absolute():
        npz_path = out_dir / npz_path
    npz_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = npz_path.with_name(f".{npz_path.name}.{os.getpid()}.tmp.npz")
    try:
        np.savez_compressed(
            str(tmp_path),
            symbols=np.asarray([str(args.live_symbol or DEFAULT_LIVE_SYMBOL)], dtype="<U64"),
            offsets=np.asarray([0, len(rows)], dtype=np.int64),
            timestamp_s=np.asarray([r["timestamp_s"] for r in rows], dtype=np.int64),
            open=np.asarray([r["open"] for r in rows], dtype=np.float64),
            high=np.asarray([r["high"] for r in rows], dtype=np.float64),
            low=np.asarray([r["low"] for r in rows], dtype=np.float64),
            close=np.asarray([r["close"] for r in rows], dtype=np.float64),
            volume=np.asarray([r["volume"] for r in rows], dtype=np.float64),
        )
        os.replace(str(tmp_path), str(npz_path))
        return str(npz_path)
    except Exception:
        try:
            tmp_path.unlink()
        except Exception:
            pass
        return None


def _load_order_extra(row: Dict[str, Any]) -> Dict[str, Any]:
    raw = row.get("extra")
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _order_fee_from_extra(extra: Dict[str, Any]) -> float:
    for container_key in ("submitted", "fill", "closed"):
        container = extra.get(container_key)
        if not isinstance(container, dict):
            continue
        order = container.get("order") or container.get("live_order") or container.get("live_exit_order")
        if isinstance(order, dict):
            fee = order_fee_usdt(order)
            if fee:
                return fee
    return 0.0


def _chart_event_type(order_type: Any, reason: Any, extra: Dict[str, Any]) -> str:
    order_type_text = str(order_type or "").upper()
    reason_text = str(reason or "")
    fill = extra.get("fill") if isinstance(extra.get("fill"), dict) else {}
    fill_type = str(fill.get("fill_type") or "")
    if order_type_text in {"CLOSE", "EXIT"}:
        return "meta_full_close"
    if fill_type.startswith("dca_add") or reason_text == "mark_crossed_dca_level":
        return "dca_buy"
    return "meta_open"


def _chart_event_pnl(extra: Dict[str, Any]) -> str:
    for container_key in ("closed", "fill"):
        container = extra.get(container_key)
        if isinstance(container, dict) and "paper_pnl_usdt" in container:
            return _fmt_float(container.get("paper_pnl_usdt"))
    return ""


def _chart_event_text(row: Dict[str, Any], extra: Dict[str, Any]) -> str:
    bits = []
    reason = str(row.get("reason") or "")
    if reason:
        bits.append(reason)
    fill = extra.get("fill") if isinstance(extra.get("fill"), dict) else {}
    fill_type = str(fill.get("fill_type") or "")
    if fill_type:
        bits.append(fill_type)
    status = str(row.get("status") or "")
    if status:
        bits.append(status)
    return " | ".join(bits)


def _read_source_margin_chart_events(args: argparse.Namespace) -> List[Dict[str, Any]]:
    state_path = str(getattr(args, "state_path", "") or "")
    if not state_path or not Path(state_path).exists():
        return []
    try:
        with Path(state_path).open("r", encoding="utf-8") as fh:
            state = json.load(fh)
    except Exception:
        return []
    observations = state.get("source_size_observations") if isinstance(state, dict) else []
    if not isinstance(observations, list):
        return []
    out: List[Dict[str, Any]] = []
    for idx, item in enumerate(observations):
        if not isinstance(item, dict):
            continue
        event_type = str(item.get("type") or "")
        if event_type not in {"source_margin_add_not_followed", "source_size_increase_clamped"}:
            continue
        ts = str(item.get("utc") or item.get("ts") or "")
        if not ts:
            continue
        if event_type == "source_size_increase_clamped":
            sync = item.get("source_size_sync") if isinstance(item.get("source_size_sync"), dict) else {}
            requested = item.get("requested_notional") or sync.get("requested_delta_notional") or sync.get("delta_notional")
            clamped = item.get("clamped_notional") or sync.get("clamped_delta_notional")
            text_bits = ["SOURCE SIZE ADD BLOCKED"]
            if requested not in (None, ""):
                text_bits.append(f"requested={_fmt_float(requested)} USDT")
            if clamped not in (None, ""):
                text_bits.append(f"allowed={_fmt_float(clamped)} USDT")
        else:
            delta = item.get("source_margin_delta_usdt")
            change_pct = item.get("source_margin_change_pct")
            text_bits = ["TRADER ADDED MARGIN; FOLLOWER DID NOT"]
            if delta not in (None, ""):
                text_bits.append(f"delta={_fmt_float(delta)} USDT")
            if change_pct not in (None, ""):
                text_bits.append(f"change={_fmt_float(change_pct)}%")
        reason = str(item.get("reason") or "")
        if reason:
            text_bits.append(reason)
        out.append(
            {
                "ts": ts,
                "type": event_type,
                "side": item.get("side") or "",
                "symbol": item.get("symbol") or args.live_symbol or DEFAULT_LIVE_SYMBOL,
                "price": _fmt_float(item.get("price") or 0.0),
                "qty": "",
                "order_id": f"source-follow-missed-{idx}",
                "position_id": item.get("key") or "",
                "pnl": "",
                "status": "OBSERVED",
                "text": " | ".join(text_bits),
                "color": "#EF4444",
                "label_color": "#EF4444",
            }
        )
    return out[-10000:]


def export_live_chart_events(args: argparse.Namespace) -> Dict[str, Optional[str]]:
    rows = _read_session_orders_for_artifacts(str(args.session_db or ""), str(args.run_id or ""))
    source_margin_events = _read_source_margin_chart_events(args)
    if not rows and not source_margin_events:
        return {"live_chart_events_jsonl": None, "live_chart_events_csv": None}
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    events: List[Dict[str, Any]] = []
    for row in rows:
        extra = _load_order_extra(row)
        event_type = _chart_event_type(row.get("type"), row.get("reason"), extra)
        fill = extra.get("fill") if isinstance(extra.get("fill"), dict) else {}
        submitted = extra.get("submitted") if isinstance(extra.get("submitted"), dict) else {}
        position_id = str(fill.get("key") or submitted.get("position_id") or f"{row.get('symbol') or args.live_symbol}:{row.get('side') or ''}")
        events.append(
            {
                "ts": row.get("ts_utc") or "",
                "type": event_type,
                "side": row.get("side") or "",
                "symbol": row.get("symbol") or args.live_symbol or DEFAULT_LIVE_SYMBOL,
                "price": _fmt_float(row.get("price")),
                "qty": _fmt_float(row.get("qty")),
                "order_id": row.get("order_id") or "",
                "position_id": position_id,
                "pnl": _chart_event_pnl(extra),
                "status": row.get("status") or "",
                "text": _chart_event_text(row, extra),
                "color": "",
                "label_color": "",
            }
        )
    events.extend(source_margin_events)
    events.sort(key=lambda item: str(item.get("ts") or ""))
    jsonl_path = out_dir / "live_chart_events.jsonl"
    csv_path = out_dir / "live_chart_events.csv"
    with jsonl_path.open("w", encoding="utf-8") as fh:
        for event in events[-10000:]:
            fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["ts", "type", "side", "symbol", "price", "qty", "order_id", "position_id", "pnl", "status", "text", "color", "label_color"],
        )
        writer.writeheader()
        writer.writerows(events[-10000:])
    return {"live_chart_events_jsonl": str(jsonl_path), "live_chart_events_csv": str(csv_path)}


def write_live_strategy_params_artifact(args: argparse.Namespace, status: Dict[str, Any]) -> str:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "live_strategy_params.json"
    payload = {
        "schema": "hype_cap100_live_strategy_params_v1",
        "utc": str(status.get("utc") or paper.iso(paper.utc_now())),
        "run_id": args.run_id,
        "mode": "hype_cap100_bingx_live_canary",
        "strategy_version": "hype_cap100_live_v1",
        "candidate_index": paper.CHAMPION_CANDIDATE_INDEX,
        "active_params": paper.CHAMPION_PARAMS,
        "symbol": args.symbol,
        "live_symbol": args.live_symbol,
        "exchange": args.live_exchange,
        "live_exchange_profile": args.live_exchange_profile,
        "timeframe": _artifact_timeframe(args),
        "copy_poll_interval_sec": args.interval_sec,
        "dca_eval_interval_sec": args.dca_eval_interval_sec,
        "history_poll_interval_sec": args.history_poll_interval_sec,
        "position_mode": args.position_mode,
        "source_leverage_policy": {
            "source_leverage_mode": getattr(args, "source_leverage_mode", "ignore"),
            "source_margin_mode_override": getattr(args, "source_margin_mode_override", ""),
            "fixed_source_leverage": getattr(args, "fixed_source_leverage", 0.0),
            "max_source_leverage": getattr(args, "max_source_leverage", 0.0),
        },
        "long_only": bool(args.long_only),
        "risk": {
            "initial_equity": args.initial_equity,
            "initial_target_notional": args.initial_target_notional,
            "max_gross_notional_usdt": args.max_gross_notional_usdt,
            "max_one_side_notional_usdt": args.max_one_side_notional_usdt,
            "max_daily_loss_usdt": args.max_daily_loss_usdt,
            "max_orders_per_hour": args.max_orders_per_hour,
            "max_order_attempts_per_hour": getattr(args, "max_order_attempts_per_hour", DEFAULT_MAX_ORDER_ATTEMPTS_PER_HOUR),
            "deadline_utc": args.deadline_utc,
        },
        "protection": {
            "account_loss_stop_usdt": getattr(args, "protection_account_loss_stop_usdt", 0.0),
            "floating_pnl_stop_usdt": getattr(args, "protection_floating_pnl_stop_usdt", 0.0),
            "emergency_account_loss_usdt": getattr(args, "protection_emergency_account_loss_usdt", 0.0),
            "stale_market_sec": getattr(args, "protection_stale_market_sec", 0.0),
            "require_book_ok": bool(getattr(args, "protection_require_book_ok", False)),
            "require_premium_ok": bool(getattr(args, "protection_require_premium_ok", False)),
            "auto_stop_new_orders": bool(getattr(args, "protection_auto_stop_new_orders", False)),
        },
    }
    paper.write_json(path, payload)
    return str(path)


def emit_ui_artifacts(args: argparse.Namespace, status: Dict[str, Any]) -> Dict[str, Optional[str]]:
    live_candles_csv = write_live_candles_artifact(args, status)
    artifacts = {
        "live_equity_csv": write_live_equity_artifacts(args, status),
        "match_ready_csv": export_match_ready_trade_history(args),
        "live_candles_csv": live_candles_csv,
        "live_cache_npz": write_live_cache_npz_artifact(args, live_candles_csv),
        "live_strategy_params_json": write_live_strategy_params_artifact(args, status),
    }
    artifacts.update(export_live_chart_events(args))
    return artifacts


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


def _positive_arg(args: argparse.Namespace, name: str) -> float:
    try:
        value = float(getattr(args, name, 0.0) or 0.0)
    except Exception:
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


def _age_sec(now: datetime, iso_raw: Any) -> Optional[float]:
    if not iso_raw:
        return None
    try:
        dt = paper.parse_utc(str(iso_raw))
        return max(0.0, (now - dt).total_seconds())
    except Exception:
        return None


def open_unrealized_pnl_usdt(state: Dict[str, Any], mark: Optional[float]) -> float:
    total = 0.0
    open_trades = state.get("open_trades") if isinstance(state.get("open_trades"), dict) else {}
    for trade in open_trades.values():
        if not isinstance(trade, dict):
            continue
        try:
            total += float(paper.unrealized_pnl(trade, mark))
        except Exception:
            continue
    return total


def evaluate_live_protection(
    state: Dict[str, Any],
    mark: Optional[float],
    now: datetime,
    input_meta: Dict[str, Any],
    args: argparse.Namespace,
) -> Dict[str, Any]:
    reasons: List[Dict[str, Any]] = []
    daily_pnl = float(paper.daily_pnl_usdt(state, mark, now))
    floating_pnl = open_unrealized_pnl_usdt(state, mark)

    account_loss_stop = _positive_arg(args, "protection_account_loss_stop_usdt")
    if account_loss_stop and daily_pnl <= -account_loss_stop:
        reasons.append({"code": "account_loss_stop", "value": daily_pnl, "threshold": -account_loss_stop})

    floating_stop = _positive_arg(args, "protection_floating_pnl_stop_usdt")
    if floating_stop and floating_pnl <= -floating_stop:
        reasons.append({"code": "floating_pnl_stop", "value": floating_pnl, "threshold": -floating_stop})

    emergency_loss = _positive_arg(args, "protection_emergency_account_loss_usdt")
    emergency = bool(emergency_loss and daily_pnl <= -emergency_loss)
    if emergency:
        reasons.append({"code": "emergency_account_loss", "value": daily_pnl, "threshold": -emergency_loss})

    stale_limit = _positive_arg(args, "protection_stale_market_sec")
    market_meta = input_meta.get("market") if isinstance(input_meta.get("market"), dict) else {}
    positions_meta = input_meta.get("positions") if isinstance(input_meta.get("positions"), dict) else {}
    if stale_limit:
        mark_age = _age_sec(now, state.get("last_mark_poll_utc"))
        if mark_age is None or mark_age > stale_limit:
            reasons.append({"code": "stale_mark", "age_sec": mark_age, "threshold_sec": stale_limit, "market": market_meta})
        if market_meta.get("error") and (mark_age is None or mark_age > stale_limit):
            reasons.append({"code": "mark_fetch_error", "error": str(market_meta.get("error"))[:240], "age_sec": mark_age})
        pos_age = _age_sec(now, state.get("last_positions_poll_utc"))
        if positions_meta.get("error") and (pos_age is None or pos_age > stale_limit):
            reasons.append({"code": "positions_fetch_error_stale", "error": str(positions_meta.get("error"))[:240], "age_sec": pos_age})

    if getattr(args, "protection_require_book_ok", False) and market_meta.get("book_ok") is False:
        reasons.append({"code": "book_not_ok", "market": market_meta})
    if getattr(args, "protection_require_premium_ok", False) and market_meta.get("premium_ok") is False:
        reasons.append({"code": "premium_not_ok", "market": market_meta})

    block_new_entries = bool(reasons)
    return {
        "schema": "hype_live_protection_v1",
        "active": block_new_entries,
        "block_new_entries": block_new_entries,
        "emergency": emergency,
        "daily_realized_plus_unrealized_pnl_usdt": daily_pnl,
        "floating_pnl_usdt": floating_pnl,
        "mark": mark,
        "thresholds": {
            "account_loss_stop_usdt": account_loss_stop,
            "floating_pnl_stop_usdt": floating_stop,
            "emergency_account_loss_usdt": emergency_loss,
            "stale_market_sec": stale_limit,
            "require_book_ok": bool(getattr(args, "protection_require_book_ok", False)),
            "require_premium_ok": bool(getattr(args, "protection_require_premium_ok", False)),
        },
        "auto_stop_new_orders": bool(getattr(args, "protection_auto_stop_new_orders", False)),
        "reasons": reasons,
    }


def apply_live_protection_side_effects(args: argparse.Namespace, protection: Dict[str, Any], now: datetime) -> Optional[Dict[str, Any]]:
    if not protection.get("block_new_entries") or not getattr(args, "protection_auto_stop_new_orders", False):
        return None
    stop_path, _kill_path = control_paths(args)
    if stop_path.exists():
        return {"type": "protection_stop_already_present", "path": str(stop_path), "protection": protection}
    stop_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "utc": paper.iso(now),
        "run_id": args.run_id,
        "reason": "auto_protection_block_new_entries",
        "protection": protection,
    }
    stop_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return {"type": "protection_stop_new_orders_created", "path": str(stop_path), "protection": protection}


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
            "live_exchange_profile": args.live_exchange_profile,
            "live_exchange": args.live_exchange,
            "live_symbol": args.live_symbol,
            "position_mode": args.position_mode,
            "initial_equity": args.initial_equity,
            "initial_target_notional": args.initial_target_notional,
            "max_gross_notional_usdt": args.max_gross_notional_usdt,
            "max_one_side_notional_usdt": args.max_one_side_notional_usdt,
            "max_daily_loss_usdt": args.max_daily_loss_usdt,
            "max_orders_per_hour": args.max_orders_per_hour,
            "max_order_attempts_per_hour": getattr(args, "max_order_attempts_per_hour", DEFAULT_MAX_ORDER_ATTEMPTS_PER_HOUR),
            "deadline_utc": args.deadline_utc,
            "protection_account_loss_stop_usdt": getattr(args, "protection_account_loss_stop_usdt", 0.0),
            "protection_floating_pnl_stop_usdt": getattr(args, "protection_floating_pnl_stop_usdt", 0.0),
            "protection_emergency_account_loss_usdt": getattr(args, "protection_emergency_account_loss_usdt", 0.0),
            "protection_stale_market_sec": getattr(args, "protection_stale_market_sec", 0.0),
            "protection_require_book_ok": bool(getattr(args, "protection_require_book_ok", False)),
            "protection_require_premium_ok": bool(getattr(args, "protection_require_premium_ok", False)),
            "protection_auto_stop_new_orders": bool(getattr(args, "protection_auto_stop_new_orders", False)),
            "source_leverage_mode": getattr(args, "source_leverage_mode", "ignore"),
            "source_margin_mode_override": getattr(args, "source_margin_mode_override", ""),
            "fixed_source_leverage": getattr(args, "fixed_source_leverage", 0.0),
            "max_source_leverage": getattr(args, "max_source_leverage", 0.0),
            "long_only": bool(args.long_only),
            "interval_sec": args.interval_sec,
            "dca_eval_interval_sec": args.dca_eval_interval_sec,
            "history_poll_interval_sec": args.history_poll_interval_sec,
            "mark_poll_interval_sec": getattr(args, "mark_poll_interval_sec", DEFAULT_MARK_POLL_INTERVAL_SEC),
            "order_sync_wait_sec": args.order_sync_wait_sec,
            "order_sync_poll_sec": args.order_sync_poll_sec,
            "order_post_throttle_sec": getattr(args, "order_post_throttle_sec", DEFAULT_ORDER_POST_THROTTLE_SEC),
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


def _truthy_config_value(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def load_live_config(path: str) -> Dict[str, Any]:
    if not _truthy_config_value(path):
        return {}
    cfg_path = Path(path)
    if not cfg_path.exists():
        raise SystemExit(f"--live-config does not exist: {path}")
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"failed to read --live-config {path}: {exc}") from exc
    if not isinstance(cfg, dict):
        raise SystemExit(f"--live-config must contain a JSON object: {path}")
    cfg["_config_path"] = str(cfg_path)
    return cfg


def _compact_live_symbol(value: Any) -> str:
    text = str(value or "").upper().strip()
    if not text:
        return ""
    return text.replace("/", "").replace("-", "").replace(":", "")


def _meta_exchange_from_args(args: argparse.Namespace, cfg: Dict[str, Any]) -> str:
    explicit = str(getattr(args, "live_exchange", "") or cfg.get("live_exchange") or "").lower().strip()
    if explicit:
        return explicit
    profile = str(getattr(args, "live_exchange_profile", "") or cfg.get("live_exchange_profile") or cfg.get("exchange_profile") or "").lower()
    for name, payload in LIVE_EXCHANGE_PROFILES.items():
        if profile == name.lower():
            return str(payload.get("live_exchange") or "").lower()
    return ""


def expand_callme_meta_live_config(args: argparse.Namespace, cfg: Dict[str, Any]) -> Dict[str, Any]:
    exchange = _meta_exchange_from_args(args, cfg)
    exchanges = cfg.get("exchanges") if isinstance(cfg.get("exchanges"), dict) else {}
    exchange_cfg = exchanges.get(exchange) if isinstance(exchanges.get(exchange), dict) else {}
    if not exchange or not exchange_cfg:
        raise SystemExit("Callme meta-strategy live config requires --live-exchange matching an enabled exchange")
    if exchange_cfg.get("enabled") is False:
        raise SystemExit(f"Callme meta-strategy exchange is disabled: {exchange}")

    allocation = cfg.get("allocation") if isinstance(cfg.get("allocation"), dict) else {}
    exchange_allocation = exchange_cfg.get("allocation") if isinstance(exchange_cfg.get("allocation"), dict) else {}
    default_symbol = cfg.get("default_symbol_config") if isinstance(cfg.get("default_symbol_config"), dict) else {}
    sizing = default_symbol.get("sizing") if isinstance(default_symbol.get("sizing"), dict) else {}
    safety = default_symbol.get("safety") if isinstance(default_symbol.get("safety"), dict) else {}
    protection = default_symbol.get("protection") if isinstance(default_symbol.get("protection"), dict) else {}
    source_leverage = default_symbol.get("source_leverage") if isinstance(default_symbol.get("source_leverage"), dict) else {}
    source_size_sync = default_symbol.get("source_size_sync") if isinstance(default_symbol.get("source_size_sync"), dict) else {}
    env = exchange_cfg.get("env") if isinstance(exchange_cfg.get("env"), dict) else {}
    lead = cfg.get("lead") if isinstance(cfg.get("lead"), dict) else {}

    margin = (
        exchange_allocation.get("initial_equity_usdt")
        or exchange_allocation.get("exchange_margin_usdt")
        or exchange_allocation.get("max_notional_usdt")
        or allocation.get("default_exchange_margin_usdt")
        or allocation.get("default_max_notional_usdt")
        or 0.0
    )
    max_notional = exchange_allocation.get("max_notional_usdt") or exchange_allocation.get("initial_target_notional_usdt") or allocation.get("default_max_notional_usdt") or margin
    max_one_side = exchange_allocation.get("max_one_side_notional_usdt") or max_notional
    initial_target = exchange_allocation.get("initial_target_notional_usdt") or max_notional
    constraints: Dict[str, Dict[str, Any]] = {}
    first_live_symbol = ""
    for symbol, symbol_cfg in (cfg.get("symbols") or {}).items():
        if str(symbol) == "*" or not isinstance(symbol_cfg, dict):
            continue
        exchange_symbols = symbol_cfg.get("exchange_symbols") if isinstance(symbol_cfg.get("exchange_symbols"), dict) else {}
        item = exchange_symbols.get(exchange) if isinstance(exchange_symbols.get(exchange), dict) else {}
        if not item or item.get("available") is False:
            continue
        live_symbol = str(item.get("live_symbol") or "").strip()
        if live_symbol and not first_live_symbol:
            first_live_symbol = live_symbol
        constraint = {k: v for k, v in item.items() if k in {"min_coin_qty", "min_base_qty", "min_order_qty_coin", "contract_size_coin", "min_contracts"}}
        if constraint:
            constraints[str(symbol)] = constraint
            if live_symbol:
                constraints[live_symbol] = constraint

    expanded = {
        "_config_path": cfg.get("_config_path"),
        "_meta_strategy": cfg.get("name") or "callme_meta_strategy_live",
        "name": f"{cfg.get('name') or 'callme_meta_strategy_live'}_{exchange}",
        "live_exchange": exchange,
        "live_exchange_profile": exchange_cfg.get("exchange_profile"),
        "exchange_profile": exchange_cfg.get("exchange_profile"),
        "live_symbol": first_live_symbol or DEFAULT_LIVE_SYMBOL,
        "position_mode": exchange_cfg.get("position_mode"),
        "env": env,
        "portfolio_id": lead.get("portfolio_id"),
        "signal": {"portfolio_id": lead.get("portfolio_id"), "copy_symbol": "*"},
        "allocation": {
            "initial_equity_usdt": margin,
            "initial_target_notional_usdt": initial_target,
            "max_notional_usdt": max_notional,
            "max_one_side_notional_usdt": max_one_side,
        },
        "sizing": sizing,
        "safety": safety,
        "protection": protection,
        "source_leverage": source_leverage,
        "source_size_sync": source_size_sync,
        "poll_sec": safety.get("poll_sec"),
        "dca_eval_interval_sec": sizing.get("dca_eval_interval_sec"),
        "mark_poll_interval_sec": safety.get("mark_poll_interval_sec"),
        "history_poll_interval_sec": safety.get("history_poll_interval_sec"),
        "symbol_market_constraints": constraints,
        "callme_meta_symbols": cfg.get("symbols") if isinstance(cfg.get("symbols"), dict) else {},
        "live_ack_env": exchange_cfg.get("live_ack_env"),
        "live_ack_value": exchange_cfg.get("live_ack_value"),
    }
    return expanded


def apply_live_config(args: argparse.Namespace) -> Dict[str, Any]:
    cfg = load_live_config(getattr(args, "live_config", "") or "")
    if not cfg:
        args._live_config = {}
        return {"config": "", "defaults_applied": []}
    if str(cfg.get("schema") or "") == "callme_meta_strategy_config_v1":
        cfg = expand_callme_meta_live_config(args, cfg)
    env = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}
    allocation = cfg.get("allocation") if isinstance(cfg.get("allocation"), dict) else {}
    sizing = cfg.get("sizing") if isinstance(cfg.get("sizing"), dict) else {}
    safety = cfg.get("safety") if isinstance(cfg.get("safety"), dict) else {}
    protection = cfg.get("protection") if isinstance(cfg.get("protection"), dict) else {}
    source_size_sync = cfg.get("source_size_sync") if isinstance(cfg.get("source_size_sync"), dict) else {}
    signal = cfg.get("signal") if isinstance(cfg.get("signal"), dict) else {}
    protection_equity = (
        allocation.get("max_notional_usdt")
        or allocation.get("initial_equity_usdt")
        or cfg.get("initial_equity_usdt")
        or cfg.get("paper_notional_usdt")
    )

    def protection_usdt(abs_key: str, pct_key: str) -> Optional[float]:
        value = protection.get(abs_key)
        if _truthy_config_value(value):
            return float(value)
        pct = protection.get(pct_key)
        if not _truthy_config_value(pct) or not _truthy_config_value(protection_equity):
            return None
        return float(protection_equity) * float(pct) / 100.0

    config_defaults = {
        "live_exchange_profile": cfg.get("live_exchange_profile") or cfg.get("exchange_profile"),
        "live_exchange": cfg.get("live_exchange") or cfg.get("exchange"),
        "live_symbol": cfg.get("live_symbol") or cfg.get("symbol"),
        "position_mode": cfg.get("position_mode"),
        "env_file": env.get("file") or cfg.get("env_file"),
        "live_api_key_env": env.get("api_key_env") or env.get("key_env"),
        "live_api_secret_env": env.get("api_secret_env") or env.get("secret_env"),
        "portfolio_id": cfg.get("portfolio_id") or signal.get("portfolio_id"),
        "symbol": signal.get("copy_symbol") or signal.get("symbol"),
        "initial_equity": allocation.get("initial_equity_usdt") or allocation.get("max_notional_usdt"),
        "initial_target_notional": allocation.get("initial_target_notional_usdt") or allocation.get("max_notional_usdt"),
        "max_gross_notional_usdt": allocation.get("max_notional_usdt"),
        "max_one_side_notional_usdt": allocation.get("max_one_side_notional_usdt") or allocation.get("max_notional_usdt"),
        "max_daily_loss_usdt": safety.get("max_daily_loss_usdt"),
        "max_orders_per_hour": safety.get("max_orders_per_hour"),
        "max_order_attempts_per_hour": safety.get("max_order_attempts_per_hour"),
        "order_error_backoff_sec": safety.get("order_error_backoff_sec"),
        "order_error_circuit_sec": safety.get("order_error_circuit_sec"),
        "order_error_max_consecutive": safety.get("order_error_max_consecutive"),
        "order_post_throttle_sec": safety.get("order_post_throttle_sec"),
        "entry_failure_cooldown_sec": safety.get("entry_failure_cooldown_sec"),
        "interval_sec": cfg.get("poll_sec") or safety.get("poll_sec"),
        "dca_eval_interval_sec": cfg.get("dca_eval_interval_sec") or sizing.get("dca_eval_interval_sec"),
        "history_poll_interval_sec": cfg.get("history_poll_interval_sec"),
        "mark_poll_interval_sec": cfg.get("mark_poll_interval_sec") or safety.get("mark_poll_interval_sec"),
        "source_leverage_mode": cfg.get("source_leverage_mode") or (cfg.get("source_leverage") if isinstance(cfg.get("source_leverage"), str) else None) or ((cfg.get("source_leverage") or {}).get("mode") if isinstance(cfg.get("source_leverage"), dict) else None),
        "max_source_leverage": cfg.get("max_source_leverage") or ((cfg.get("source_leverage") or {}).get("max_source_leverage") if isinstance(cfg.get("source_leverage"), dict) else None),
        "fixed_source_leverage": cfg.get("fixed_source_leverage") or ((cfg.get("source_leverage") or {}).get("fixed_leverage") if isinstance(cfg.get("source_leverage"), dict) else None),
        "source_margin_mode_override": cfg.get("source_margin_mode_override")
        or ((cfg.get("source_leverage") or {}).get("margin_mode_override") if isinstance(cfg.get("source_leverage"), dict) else None)
        or ((cfg.get("source_leverage") or {}).get("margin_mode") if isinstance(cfg.get("source_leverage"), dict) else None),
        "source_size_sync_mode": cfg.get("source_size_sync_mode") or source_size_sync.get("mode"),
        "source_size_sync_interval_sec": cfg.get("source_size_sync_interval_sec") or source_size_sync.get("interval_sec"),
        "source_size_sync_min_change_pct": cfg.get("source_size_sync_min_change_pct") or source_size_sync.get("min_change_pct"),
        "source_size_sync_min_adjust_notional_usdt": cfg.get("source_size_sync_min_adjust_notional_usdt") or source_size_sync.get("min_adjust_notional_usdt"),
        "protection_account_loss_stop_usdt": protection_usdt("account_loss_stop_usdt", "account_loss_stop_pct_of_equity"),
        "protection_floating_pnl_stop_usdt": protection_usdt("floating_pnl_stop_usdt", "floating_pnl_stop_pct_of_equity"),
        "protection_emergency_account_loss_usdt": protection_usdt("emergency_account_loss_usdt", "emergency_account_loss_pct_of_equity"),
        "protection_stale_market_sec": protection.get("stale_market_sec"),
        "protection_require_book_ok": protection.get("require_book_ok"),
        "protection_require_premium_ok": protection.get("require_premium_ok"),
        "protection_auto_stop_new_orders": protection.get("auto_stop_new_orders"),
    }
    applied: List[str] = []
    for attr, value in config_defaults.items():
        if not _truthy_config_value(value):
            continue
        setattr(args, attr, value)
        applied.append(attr)
    args._live_config = cfg
    return {"config": cfg.get("_config_path") or str(getattr(args, "live_config", "")), "defaults_applied": applied}


def _compact_symbol_key(value: Any) -> str:
    text = str(value or "").upper().strip()
    if not text:
        return ""
    return text.replace("/", "").replace("-", "").replace(":", "")


def configured_symbol_market_constraint(args: argparse.Namespace, symbol: str, ccxt_symbol: str) -> Dict[str, Any]:
    cfg = getattr(args, "_live_config", {}) or {}
    constraints = cfg.get("symbol_market_constraints") or cfg.get("market_constraints") or {}
    if not isinstance(constraints, dict):
        return {}
    candidates = [
        symbol,
        ccxt_symbol,
        getattr(args, "symbol", ""),
        getattr(args, "live_symbol", ""),
        str(symbol or "").replace("USDT", "/USDT:USDT"),
    ]
    for candidate in candidates:
        if candidate in constraints and isinstance(constraints[candidate], dict):
            return constraints[candidate]
    compact_candidates = {_compact_symbol_key(candidate) for candidate in candidates}
    for key, value in constraints.items():
        if _compact_symbol_key(key) in compact_candidates and isinstance(value, dict):
            return value
    return {}


def configured_min_base_qty(args: argparse.Namespace, symbol: str, ccxt_symbol: str) -> Tuple[float, str]:
    constraint = configured_symbol_market_constraint(args, symbol, ccxt_symbol)
    for key in ("min_coin_qty", "min_base_qty", "min_order_qty_coin"):
        try:
            value = float(constraint.get(key) or 0.0)
        except Exception:
            value = 0.0
        if value > 0:
            return value, key
    return 0.0, ""


def live_multi_symbol_mode(args: argparse.Namespace) -> bool:
    cfg = getattr(args, "_live_config", {}) or {}
    symbol = str(getattr(args, "symbol", "") or "").upper().strip()
    return bool(cfg.get("_meta_strategy")) or symbol in {"", "*", "ALL", "ANY", "MULTI", "MULTI_SYMBOL"} or "," in symbol


def resolve_live_trade_symbol(args: argparse.Namespace, client: CCXTFetcher, symbol: Any) -> Tuple[str, Dict[str, Any]]:
    requested = str(symbol or "").strip()
    resolved = client.resolve_symbol(requested) if requested else ""
    meta = {
        "requested_symbol": requested,
        "resolved_symbol": resolved,
        "multi_symbol_mode": live_multi_symbol_mode(args),
    }
    if resolved:
        return resolved, meta
    if live_multi_symbol_mode(args):
        meta["error"] = "live_symbol_unresolved_in_multi_symbol_mode"
        return "", meta
    fallback = client.resolve_symbol(args.live_symbol) or args.live_symbol
    meta["fallback_live_symbol"] = fallback
    return fallback, meta


def map_live_credential_aliases(args: argparse.Namespace) -> Dict[str, Any]:
    exchange = str(args.live_exchange or "").upper()
    key_env = str(getattr(args, "live_api_key_env", "") or "").strip()
    secret_env = str(getattr(args, "live_api_secret_env", "") or "").strip()
    target_key = f"{exchange}_KEY" if exchange else ""
    target_secret = f"{exchange}_SECRET" if exchange else ""
    out = {
        "api_key_env": key_env,
        "api_secret_env": secret_env,
        "target_key_env": target_key,
        "target_secret_env": target_secret,
        "key_mapped": False,
        "secret_mapped": False,
    }
    if key_env and target_key and os.environ.get(key_env):
        if os.environ.get(target_key) != os.environ.get(key_env):
            os.environ[target_key] = os.environ[key_env]
            out["key_mapped"] = True
    if secret_env and target_secret and os.environ.get(secret_env):
        if os.environ.get(target_secret) != os.environ.get(secret_env):
            os.environ[target_secret] = os.environ[secret_env]
            out["secret_mapped"] = True
    return out


def rebuild_fetcher_symbol_index(client: CCXTFetcher) -> None:
    client.by_base = {}
    for m in (getattr(client, "markets", {}) or {}).values():
        try:
            if m.get("swap") and m.get("quote") == "USDT":
                base = m.get("base")
                if base:
                    client.by_base[base] = m["symbol"]
        except Exception:
            continue


def configure_live_fetcher(args: argparse.Namespace, client: CCXTFetcher) -> None:
    if str(args.live_exchange or "").lower() not in {"htx", "mexc"}:
        return
    try:
        client.ex.options = dict(getattr(client.ex, "options", {}) or {})
        client.ex.options["defaultType"] = "swap"
        client.markets = client.ex.load_markets(True)
        rebuild_fetcher_symbol_index(client)
    except Exception as exc:
        raise SystemExit(f"failed to load {args.live_exchange} swap markets: {exc}") from exc


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


def order_filled_qty(order: Dict[str, Any], fallback: float = 0.0) -> float:
    for key in ("filled", "amount"):
        try:
            value = float(order.get(key) or 0.0)
        except Exception:
            value = 0.0
        if math.isfinite(value) and value > 0:
            return value
    return float(fallback or 0.0)


def order_fee_usdt(order: Dict[str, Any]) -> float:
    fees: List[Dict[str, Any]] = []
    if isinstance(order.get("fees"), list):
        fees.extend(f for f in order["fees"] if isinstance(f, dict))
    elif isinstance(order.get("fee"), dict):
        fees.append(order["fee"])
    total = 0.0
    for fee in fees:
        try:
            total += abs(float(fee.get("cost") or 0.0))
        except Exception:
            continue
    return total


def parse_source_leverage(raw: Any) -> Optional[float]:
    try:
        value = float(str(raw).strip())
    except Exception:
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def normalize_source_margin_mode(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    if text in {"cross", "crossed"}:
        return "cross"
    if text in {"isolated", "isolate"}:
        return "isolated"
    return text


def effective_source_leverage(args: argparse.Namespace, trade: Dict[str, Any]) -> Dict[str, Any]:
    mode = str(getattr(args, "source_leverage_mode", "ignore") or "ignore").strip().lower()
    if mode not in SUPPORTED_SOURCE_LEVERAGE_MODES:
        mode = "ignore"
    raw = trade.get("source_leverage_raw", trade.get("source_leverage"))
    source = parse_source_leverage(trade.get("source_leverage"))
    if source is None:
        source = parse_source_leverage(raw)
    margin_mode = normalize_source_margin_mode(getattr(args, "source_margin_mode_override", "")) or normalize_source_margin_mode(trade.get("source_margin_mode"))
    out = {
        "mode": mode,
        "source_leverage_raw": raw,
        "source_leverage": source,
        "source_margin_mode": margin_mode,
        "effective_leverage": None,
        "required": mode != "ignore",
    }
    if mode == "ignore":
        return out
    if mode == "fixed":
        fixed = parse_source_leverage(getattr(args, "fixed_source_leverage", None))
        if fixed is None:
            out["error"] = "missing_fixed_source_leverage"
            return out
        effective = fixed
        max_leverage = parse_source_leverage(getattr(args, "max_source_leverage", None))
        if max_leverage is not None:
            effective = min(effective, max_leverage)
        out["effective_leverage"] = max(1.0, float(effective))
        return out
    if source is None:
        out["error"] = "missing_source_leverage"
        return out
    if mode == "copy":
        effective = source
    else:
        effective = max(1.0, math.floor(source / 2.0)) if source > 1.0 else 1.0
    max_leverage = parse_source_leverage(getattr(args, "max_source_leverage", None))
    if max_leverage is not None:
        effective = min(effective, max_leverage)
    out["effective_leverage"] = max(1.0, float(effective))
    return out


def leverage_cache_key(args: argparse.Namespace, ccxt_symbol: str, side: str, margin_mode: str, leverage: float) -> str:
    position_side = str(side or "").upper() if str(getattr(args, "position_mode", "")).lower() == "hedge" else "BOTH"
    return "|".join(
        str(x or "")
        for x in (
            str(getattr(args, "live_exchange", "")).lower(),
            str(getattr(args, "live_exchange_profile", "")).lower(),
            ccxt_symbol,
            position_side,
            normalize_source_margin_mode(margin_mode),
            f"{float(leverage):g}",
        )
    )


def live_leverage_params(args: argparse.Namespace, side: str, margin_mode: str) -> Dict[str, Any]:
    ex = str(getattr(args, "live_exchange", "") or "").lower()
    mode = normalize_source_margin_mode(margin_mode)
    params: Dict[str, Any] = {}
    if ex == "gateio":
        params["settle"] = "usdt"
        if mode:
            params["marginMode"] = mode
        return params
    if ex == "bingx":
        if str(getattr(args, "position_mode", "") or "").lower() == "hedge" and side:
            params["side"] = str(side).upper()
            params["positionSide"] = str(side).upper()
        if mode:
            params["marginMode"] = mode
        return params
    if ex == "htx":
        if not mode:
            mode = "cross"
        if mode:
            params["marginMode"] = mode
        return params
    if ex == "mexc":
        params["openType"] = 1 if mode == "isolated" else 2
        params["positionType"] = 2 if str(side or "").upper() == "SHORT" else 1
        return params
    return params


def ensure_symbol_leverage(
    args: argparse.Namespace,
    state: Dict[str, Any],
    *,
    symbol: str,
    side: str,
    margin_mode: str,
    leverage: float,
    now: datetime,
) -> Dict[str, Any]:
    client = live_client(args)
    ccxt_symbol, symbol_resolution = resolve_live_trade_symbol(args, client, symbol)
    if not ccxt_symbol:
        return {"ok": False, "error": "live_symbol_unresolved", "symbol_resolution": symbol_resolution}
    key = leverage_cache_key(args, ccxt_symbol, side, margin_mode, leverage)
    cache = state.setdefault("leverage_set_cache", {})
    if isinstance(cache.get(key), dict) and cache[key].get("ok"):
        return {**cache[key], "cached": True, "cache_key": key}
    params = live_leverage_params(args, side, margin_mode)
    payload = {
        "ok": False,
        "cached": False,
        "exchange": getattr(args, "live_exchange", ""),
        "ccxt_symbol": ccxt_symbol,
        "side": str(side or "").upper(),
        "margin_mode": normalize_source_margin_mode(margin_mode),
        "leverage": float(leverage),
        "params": params,
        "cache_key": key,
        "utc": paper.iso(now),
    }
    try:
        if not hasattr(client.ex, "set_leverage"):
            raise RuntimeError("exchange_client_missing_set_leverage")
        leverage_value: Any = float(leverage)
        if float(leverage_value).is_integer():
            leverage_value = int(leverage_value)
        result = client.ex.set_leverage(leverage_value, ccxt_symbol, params)
    except Exception as exc:
        payload["error"] = str(exc)
        state["last_leverage_setup"] = payload
        return payload
    payload["ok"] = True
    payload["result"] = safe_order(result) if isinstance(result, dict) else str(result)
    cache[key] = payload
    state["leverage_set_cache"] = cache
    state["last_leverage_setup"] = payload
    return payload


def signed_slip_bp(side: str, expected_price: Optional[float], fill_price: Optional[float], *, is_close: bool) -> Optional[float]:
    try:
        expected = float(expected_price or 0.0)
        fill = float(fill_price or 0.0)
    except Exception:
        return None
    if expected <= 0 or fill <= 0 or not math.isfinite(expected) or not math.isfinite(fill):
        return None
    long_side = str(side or "").upper() == "LONG"
    if is_close:
        adverse = (expected - fill) if long_side else (fill - expected)
    else:
        adverse = (fill - expected) if long_side else (expected - fill)
    return adverse / expected * 10000.0


def fill_lag_sec(now: datetime, fill_dt_raw: Optional[str]) -> Optional[float]:
    if not fill_dt_raw:
        return None
    try:
        fill_dt = paper.parse_utc(str(fill_dt_raw))
    except Exception:
        return None
    try:
        return (fill_dt - now).total_seconds()
    except Exception:
        return None


def order_error_backoff_active(state: Dict[str, Any], now: datetime) -> Optional[Dict[str, Any]]:
    backoff = state.get("order_error_backoff")
    if not isinstance(backoff, dict):
        return None
    try:
        until_ts = float(backoff.get("until_ts") or 0.0)
    except Exception:
        until_ts = 0.0
    if until_ts <= now.timestamp():
        return None
    out = dict(backoff)
    out["remaining_sec"] = max(0.0, until_ts - now.timestamp())
    return out


def clear_order_error_backoff(state: Dict[str, Any]) -> None:
    backoff = state.get("order_error_backoff")
    if isinstance(backoff, dict):
        backoff["consecutive"] = 0
        backoff["until_ts"] = 0.0
        backoff["until_utc"] = ""


def register_order_error_backoff(state: Dict[str, Any], error: str, now: datetime, args: argparse.Namespace) -> Dict[str, Any]:
    text = str(error or "unknown_order_error")
    prior = state.get("order_error_backoff") if isinstance(state.get("order_error_backoff"), dict) else {}
    consecutive = int(prior.get("consecutive") or 0) + 1
    max_consecutive = max(1, int(getattr(args, "order_error_max_consecutive", DEFAULT_ORDER_ERROR_MAX_CONSECUTIVE) or DEFAULT_ORDER_ERROR_MAX_CONSECUTIVE))
    base_delay = float(getattr(args, "order_error_backoff_sec", DEFAULT_ORDER_ERROR_BACKOFF_SEC) or DEFAULT_ORDER_ERROR_BACKOFF_SEC)
    circuit_delay = float(getattr(args, "order_error_circuit_sec", DEFAULT_ORDER_ERROR_CIRCUIT_SEC) or DEFAULT_ORDER_ERROR_CIRCUIT_SEC)
    exp_delay = base_delay * (2 ** max(0, min(consecutive - 1, 8)))
    jitter_seed = f"{getattr(args, 'run_id', '')}|{text}|{consecutive}".encode("utf-8", errors="replace")
    jitter_unit = int(hashlib.sha1(jitter_seed).hexdigest()[:8], 16) / float(0xFFFFFFFF)
    jitter_cap = min(max(base_delay * 0.25, 1.0), 300.0)
    jitter = jitter_unit * jitter_cap
    delay = exp_delay + jitter
    reason = "order_error_backoff"
    if consecutive >= max_consecutive:
        delay = max(delay, circuit_delay + jitter)
        reason = "order_error_circuit_breaker"
    until_ts = now.timestamp() + max(delay, 1.0)
    payload = {
        "reason": reason,
        "last_error": text,
        "consecutive": consecutive,
        "base_delay_sec": base_delay,
        "exponential_delay_sec": exp_delay,
        "jitter_sec": jitter,
        "delay_sec": max(delay, 1.0),
        "until_ts": until_ts,
        "until_utc": paper.iso(datetime.fromtimestamp(until_ts, tz=timezone.utc)),
    }
    state["order_error_backoff"] = payload
    return payload


def _hour_bucket(now: datetime) -> str:
    return now.strftime("%Y%m%dT%H")


def prune_order_attempts(state: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    attempts = state.setdefault("order_attempts", {})
    if not isinstance(attempts, dict):
        attempts = {}
        state["order_attempts"] = attempts
    buckets = attempts.get("hourly")
    if not isinstance(buckets, dict):
        buckets = {}
        attempts["hourly"] = buckets
    keep = {_hour_bucket(now)}
    previous = datetime.fromtimestamp(max(0.0, now.timestamp() - 3600.0), tz=timezone.utc)
    keep.add(_hour_bucket(previous))
    for key in list(buckets):
        if key not in keep:
            buckets.pop(key, None)
    return attempts


def order_attempt_guard_active(state: Dict[str, Any], now: datetime, args: argparse.Namespace) -> Optional[Dict[str, Any]]:
    attempts = prune_order_attempts(state, now)
    max_attempts = int(getattr(args, "max_order_attempts_per_hour", DEFAULT_MAX_ORDER_ATTEMPTS_PER_HOUR) or 0)
    bucket = _hour_bucket(now)
    count = int((attempts.get("hourly") or {}).get(bucket) or 0)
    if max_attempts > 0 and count >= max_attempts:
        return {
            "reason": "max_order_attempts_per_hour",
            "bucket": bucket,
            "attempts": count,
            "max_order_attempts_per_hour": max_attempts,
        }
    post_until = float(attempts.get("post_throttle_until_ts") or 0.0)
    if post_until > now.timestamp():
        return {
            "reason": "order_post_throttle",
            "until_ts": post_until,
            "until_utc": paper.iso(datetime.fromtimestamp(post_until, tz=timezone.utc)),
            "remaining_sec": max(0.0, post_until - now.timestamp()),
            "last_attempt": attempts.get("last_attempt"),
        }
    return None


def register_order_post_attempt(state: Dict[str, Any], now: datetime, args: argparse.Namespace, *, action: str, symbol: str, side: str) -> Dict[str, Any]:
    attempts = prune_order_attempts(state, now)
    bucket = _hour_bucket(now)
    hourly = attempts.setdefault("hourly", {})
    hourly[bucket] = int(hourly.get(bucket) or 0) + 1
    throttle_sec = float(getattr(args, "order_post_throttle_sec", DEFAULT_ORDER_POST_THROTTLE_SEC) or 0.0)
    until_ts = now.timestamp() + max(0.0, throttle_sec)
    payload = {
        "action": str(action),
        "symbol": str(symbol),
        "side": str(side),
        "bucket": bucket,
        "attempts": hourly[bucket],
        "max_order_attempts_per_hour": int(getattr(args, "max_order_attempts_per_hour", DEFAULT_MAX_ORDER_ATTEMPTS_PER_HOUR) or 0),
        "post_throttle_sec": throttle_sec,
        "post_throttle_until_ts": until_ts,
        "post_throttle_until_utc": paper.iso(datetime.fromtimestamp(until_ts, tz=timezone.utc)),
        "utc": paper.iso(now),
    }
    attempts["last_attempt"] = payload
    attempts["post_throttle_until_ts"] = until_ts
    attempts["post_throttle_until_utc"] = payload["post_throttle_until_utc"]
    return payload


def entry_attempt_key(trade: Dict[str, Any], fill_type: str) -> str:
    return "|".join(
        str(x or "")
        for x in (
            trade.get("symbol"),
            trade.get("side"),
            trade.get("lead_position_id"),
            fill_type,
            trade.get("next_level_idx"),
        )
    )


def entry_failure_active(state: Dict[str, Any], key: str, now: datetime) -> Optional[Dict[str, Any]]:
    failures = state.get("entry_failures") if isinstance(state.get("entry_failures"), dict) else {}
    failure = failures.get(key) if isinstance(failures.get(key), dict) else None
    if not failure:
        return None
    try:
        until_ts = float(failure.get("until_ts") or 0.0)
    except Exception:
        until_ts = 0.0
    if until_ts <= now.timestamp():
        return None
    out = dict(failure)
    out["remaining_sec"] = max(0.0, until_ts - now.timestamp())
    return out


def register_entry_failure(state: Dict[str, Any], key: str, error: str, now: datetime, args: argparse.Namespace) -> Dict[str, Any]:
    failures = state.setdefault("entry_failures", {})
    prior = failures.get(key) if isinstance(failures.get(key), dict) else {}
    attempts = int(prior.get("attempts") or 0) + 1
    error_text = str(error or "unknown_order_error")
    delay = float(getattr(args, "entry_failure_cooldown_sec", DEFAULT_ENTRY_FAILURE_COOLDOWN_SEC) or DEFAULT_ENTRY_FAILURE_COOLDOWN_SEC)
    lowered = error_text.lower()
    if any(code in lowered for code in ("109429", "101400", "109400")):
        delay = max(delay, float(getattr(args, "order_error_circuit_sec", DEFAULT_ORDER_ERROR_CIRCUIT_SEC) or DEFAULT_ORDER_ERROR_CIRCUIT_SEC))
    until_ts = now.timestamp() + max(delay, 1.0)
    payload = {
        "attempts": attempts,
        "last_error": error_text,
        "last_utc": paper.iso(now),
        "until_ts": until_ts,
        "until_utc": paper.iso(datetime.fromtimestamp(until_ts, tz=timezone.utc)),
    }
    failures[key] = payload
    return payload


def clear_entry_failure(state: Dict[str, Any], key: str) -> None:
    failures = state.get("entry_failures")
    if isinstance(failures, dict):
        failures.pop(key, None)


def reset_exchange_failures_on_switch(args: argparse.Namespace) -> Dict[str, Any]:
    if not args.state_path:
        return {"changed": False, "reason": "missing_state_path"}
    path = Path(args.state_path)
    if not path.exists():
        return {"changed": False, "reason": "state_missing"}
    state = paper.load_json(path, paper.default_state(args))
    prior_exchange = str(
        state.get("live_exchange")
        or ((state.get("last_poll") or {}).get("live_exchange") if isinstance(state.get("last_poll"), dict) else "")
        or ""
    ).lower()
    current_exchange = str(args.live_exchange or "").lower()
    if not prior_exchange or prior_exchange == current_exchange:
        state["live_exchange"] = current_exchange
        paper.write_json(path, state)
        return {"changed": False, "prior_exchange": prior_exchange, "current_exchange": current_exchange}
    had_order_backoff = bool(state.get("order_error_backoff"))
    had_entry_failures = bool(state.get("entry_failures"))
    state["order_error_backoff"] = {}
    state["entry_failures"] = {}
    state["live_exchange"] = current_exchange
    state.setdefault("events", []).append(
        {
            "utc": paper.iso(paper.utc_now()),
            "type": "exchange_failures_reset_on_switch",
            "prior_exchange": prior_exchange,
            "current_exchange": current_exchange,
            "had_order_backoff": had_order_backoff,
            "had_entry_failures": had_entry_failures,
        }
    )
    state["events"] = state.get("events", [])[-getattr(args, "max_events", 2000) :]
    paper.write_json(path, state)
    return {
        "changed": True,
        "prior_exchange": prior_exchange,
        "current_exchange": current_exchange,
        "had_order_backoff": had_order_backoff,
        "had_entry_failures": had_entry_failures,
    }


def live_client(args: argparse.Namespace) -> CCXTFetcher:
    client = getattr(args, "_live_client", None)
    if client is not None:
        return client
    loaded = load_env_file(args.env_file)
    args._env_credential_aliases = map_live_credential_aliases(args)
    client = CCXTFetcher(exchange=args.live_exchange, symbol_format="usdtm")
    configure_live_fetcher(args, client)
    args._live_client = client
    args._env_loaded_keys = sorted(k for k in loaded if any(t in k.upper() for t in ("KEY", "SECRET", "API")))
    return client


def auth_probe(args: argparse.Namespace) -> Dict[str, Any]:
    client = live_client(args)
    report = client.debug_credentials_report() if hasattr(client, "debug_credentials_report") else {}
    out = {
        "exchange": args.live_exchange,
        "live_api_key_env": getattr(args, "live_api_key_env", ""),
        "live_api_secret_env": getattr(args, "live_api_secret_env", ""),
        "credential_alias_mapping": getattr(args, "_env_credential_aliases", {}),
        "credentials_present": bool(getattr(client, "credentials_present", False)),
        "key_found": bool((report or {}).get("key_found")),
        "secret_found": bool((report or {}).get("secret_found")),
        "fetch_balance_ok": False,
    }
    if not out["credentials_present"]:
        raise SystemExit("live credentials are not present after loading env file")
    balance_params = live_balance_params(args.live_exchange)
    bal = client.ex.fetch_balance(balance_params) if balance_params else client.ex.fetch_balance()
    out["fetch_balance_ok"] = True
    out["balance_params"] = balance_params
    out["balance_keys"] = sorted(list((bal or {}).keys()))[:20] if isinstance(bal, dict) else []
    return out


def live_balance_params(exchange: str) -> Dict[str, Any]:
    if str(exchange or "").lower() == "gateio":
        return {"type": "swap", "settle": "USDT"}
    if str(exchange or "").lower() == "htx":
        return {"type": "swap", "defaultSubType": "linear", "unified": True}
    if str(exchange or "").lower() == "mexc":
        return {"type": "swap"}
    return {}


def live_order_params(client_order_id: str, side: str, reduce_only: bool, position_mode: str = "oneway", exchange: str = "bingx") -> Dict[str, Any]:
    ex = str(exchange or "").lower()
    if ex == "gateio":
        params: Dict[str, Any] = {"text": gateio_client_order_text(client_order_id)}
        if reduce_only:
            params["reduceOnly"] = True
        return params
    if ex == "htx":
        params = {"channel_code": "", "order_price_type": "optimal_20"}
        if reduce_only:
            params["reduceOnly"] = True
        return params
    if ex == "mexc":
        params = {"marginMode": "cross", "externalOid": client_order_id}
        if reduce_only:
            params["reduceOnly"] = True
        return params
    params = {"clientOrderId": client_order_id}
    if reduce_only:
        params["reduceOnly"] = True
    if str(position_mode or "").lower() == "hedge":
        params["hedged"] = True
    elif side:
        params["positionSide"] = side.upper()
    return params


def live_market_contract_size(client: CCXTFetcher, ccxt_symbol: str) -> float:
    try:
        market = client.ex.market(ccxt_symbol)
    except Exception:
        market = (getattr(client.ex, "markets", {}) or {}).get(ccxt_symbol) or {}
    for source in (market, market.get("info") if isinstance(market, dict) else {}):
        if not isinstance(source, dict):
            continue
        for key in ("contractSize", "contract_size", "quanto_multiplier"):
            try:
                val = float(source.get(key) or 0.0)
            except Exception:
                val = 0.0
            if val > 0:
                return val
    return 1.0


def live_order_amount_from_base_qty(
    args: argparse.Namespace,
    client: CCXTFetcher,
    ccxt_symbol: str,
    base_qty: float,
    *,
    is_close: bool,
    max_base_qty: Optional[float] = None,
) -> Tuple[float, float, float]:
    exchange = str(args.live_exchange).lower()
    is_gateio = exchange == "gateio"
    contract_size = live_market_contract_size(client, ccxt_symbol) if exchange in {"gateio", "htx", "mexc"} else 1.0
    raw_amount = float(base_qty or 0.0) / max(contract_size, 1e-12)
    max_amount = None if max_base_qty is None else float(max_base_qty or 0.0) / max(contract_size, 1e-12)
    order_amount = _normalize_order_qty(client, ccxt_symbol, raw_amount, is_close=is_close, max_qty=max_amount)
    if is_gateio and order_amount > 0:
        # Gate.io futures order amount is contracts. CCXT market precision can
        # report fractional precision, but the exchange floors fractional
        # contract sizes. Normalize explicitly so telemetry/preflight matches
        # what is actually sent and filled.
        if is_close:
            order_amount = math.floor(float(order_amount) + 1e-12)
            if max_amount is not None:
                order_amount = min(order_amount, math.floor(float(max_amount) + 1e-12))
        else:
            order_amount = max(1.0, float(round(float(order_amount))))
        if max_amount is not None:
            order_amount = min(order_amount, math.floor(float(max_amount) + 1e-12))
    return float(order_amount or 0.0), float(order_amount or 0.0) * contract_size, contract_size


def runtime_sizing_payload(state: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    state_equity = float(state.get("equity") or getattr(args, "initial_equity", 0.0) or 0.0)
    effective_new_entry_target = min(
        float(getattr(args, "initial_target_notional", 0.0) or 0.0),
        float(getattr(args, "max_gross_notional_usdt", 0.0) or 0.0),
        max(state_equity, 0.0),
    )
    open_trades = state.get("open_trades") if isinstance(state.get("open_trades"), dict) else {}
    open_targets = []
    for key, trade in sorted(open_trades.items()):
        if not isinstance(trade, dict):
            continue
        open_targets.append(
            {
                "key": key,
                "target_notional": float(trade.get("target_notional") or 0.0),
                "base_notional": float(trade.get("base_notional") or 0.0),
                "remaining_add_notional": sum(float(x or 0.0) for x in list(trade.get("add_notionals") or [])[int(trade.get("next_level_idx") or 0) :]),
                "current_notional": float(trade.get("notional") or 0.0),
                "qty": float(trade.get("qty") or 0.0),
            }
        )
    return {
        "cli_initial_equity": float(getattr(args, "initial_equity", 0.0) or 0.0),
        "cli_initial_target_notional": float(getattr(args, "initial_target_notional", 0.0) or 0.0),
        "cli_max_gross_notional_usdt": float(getattr(args, "max_gross_notional_usdt", 0.0) or 0.0),
        "state_initial_equity": float(state.get("initial_equity") or 0.0),
        "state_equity": state_equity,
        "effective_new_entry_target_notional": effective_new_entry_target,
        "open_trade_targets": open_targets,
    }


def live_order_filled_base_qty(args: argparse.Namespace, client: CCXTFetcher, ccxt_symbol: str, order: Dict[str, Any], fallback_order_amount: float) -> float:
    order_amount = order_filled_qty(order, fallback_order_amount)
    if str(args.live_exchange).lower() in {"gateio", "htx", "mexc"}:
        return float(order_amount or 0.0) * live_market_contract_size(client, ccxt_symbol)
    return float(order_amount or 0.0)


def htx_submit_market_order(client: CCXTFetcher, ccxt_symbol: str, order_side: str, order_amount: float, *, reduce_only: bool) -> Dict[str, Any]:
    market = client.ex.market(ccxt_symbol)
    request: Dict[str, Any] = {
        "contract_code": market["id"],
        "volume": client.ex.amount_to_precision(ccxt_symbol, order_amount),
        "direction": order_side,
        "offset": "close" if reduce_only else "open",
        "lever_rate": 1,
        "order_price_type": "optimal_20",
    }
    if reduce_only:
        request["reduce_only"] = 1
    response = client.ex.contractPrivatePostLinearSwapApiV1SwapCrossOrder(request)
    data = response.get("data") if isinstance(response, dict) else {}
    order_id = ""
    if isinstance(data, dict):
        order_id = str(data.get("order_id_str") or data.get("order_id") or "")
    return {
        "id": order_id,
        "info": response,
        "symbol": ccxt_symbol,
        "type": "market",
        "side": order_side,
        "amount": order_amount,
        "filled": order_amount,
        "remaining": 0.0,
        "status": "closed",
    }


def live_open_order_preflight(args: argparse.Namespace, symbol: str, expected_price: float, notional: float) -> Dict[str, Any]:
    client = getattr(args, "_live_client", None)
    requested_notional = float(notional or 0.0)
    expected_price = float(expected_price or 0.0)
    if client is None or expected_price <= 0 or requested_notional <= 0:
        return {"ok": True, "available": False, "requested_notional": requested_notional, "normalized_notional": requested_notional}
    ccxt_symbol, symbol_resolution = resolve_live_trade_symbol(args, client, symbol)
    if not ccxt_symbol:
        return {
            "ok": False,
            "available": True,
            "reason": "live_symbol_unresolved",
            "symbol_resolution": symbol_resolution,
            "requested_notional": requested_notional,
            "normalized_notional": requested_notional,
        }
    requested_base_qty = requested_notional / max(expected_price, 1e-12)
    min_base_qty, min_base_source = configured_min_base_qty(args, symbol, ccxt_symbol)
    effective_base_qty = max(requested_base_qty, min_base_qty)
    order_amount, normalized_base_qty, contract_size = live_order_amount_from_base_qty(args, client, ccxt_symbol, effective_base_qty, is_close=False)
    normalized_notional = normalized_base_qty * expected_price
    resize_bp = (normalized_notional - requested_notional) / max(requested_notional, 1e-12) * 10000.0
    out = {
        "ok": True,
        "available": True,
        "ccxt_symbol": ccxt_symbol,
        "symbol_resolution": symbol_resolution,
        "requested_notional": requested_notional,
        "requested_base_qty": requested_base_qty,
        "effective_base_qty": effective_base_qty,
        "configured_min_coin_qty": min_base_qty,
        "configured_min_coin_qty_source": min_base_source,
        "normalized_contract_amount": order_amount,
        "normalized_base_qty": normalized_base_qty,
        "normalized_notional": normalized_notional,
        "contract_size": contract_size,
        "normalization_resize_bp": resize_bp,
    }
    if order_amount <= 0 or normalized_base_qty <= 0:
        return {**out, "ok": False, "reason": "open_qty_zero_after_normalize"}
    return out


def live_fetch_exchange_position(args: argparse.Namespace, client: CCXTFetcher, ccxt_symbol: str, side: str) -> Optional[Dict[str, Any]]:
    pos = _fetch_exchange_position(client, ccxt_symbol, side)
    if not isinstance(pos, dict):
        return pos
    if str(args.live_exchange).lower() in {"gateio", "htx", "mexc"} and not pos.get("_qty_unit") == "base":
        out = dict(pos)
        contract_size = live_market_contract_size(client, ccxt_symbol)
        out["qty_contracts"] = float(out.get("qty") or 0.0)
        out["contract_size"] = contract_size
        out["qty"] = out["qty_contracts"] * contract_size
        out["_qty_unit"] = "base"
        return out
    return pos


def submit_open(args: argparse.Namespace, symbol: str, side: str, expected_price: float, notional: float, client_order_id: str) -> Dict[str, Any]:
    client = live_client(args)
    ccxt_symbol, symbol_resolution = resolve_live_trade_symbol(args, client, symbol)
    if not ccxt_symbol:
        return {"ok": False, "error": "live_symbol_unresolved", "symbol_resolution": symbol_resolution}
    base_qty = float(notional) / max(float(expected_price), 1e-12)
    min_base_qty, min_base_source = configured_min_base_qty(args, symbol, ccxt_symbol)
    effective_base_qty = max(base_qty, min_base_qty)
    order_amount, expected_base_qty, contract_size = live_order_amount_from_base_qty(args, client, ccxt_symbol, effective_base_qty, is_close=False)
    if order_amount <= 0:
        return {"ok": False, "error": "open_qty_zero_after_normalize", "qty": expected_base_qty, "order_amount": order_amount}
    params = live_order_params(client_order_id, side, reduce_only=False, position_mode=args.position_mode, exchange=args.live_exchange)
    order_side = "buy" if side.upper() == "LONG" else "sell"
    try:
        if str(args.live_exchange or "").lower() == "htx":
            od = htx_submit_market_order(client, ccxt_symbol, order_side, order_amount, reduce_only=False)
        else:
            od = client.ex.create_order(ccxt_symbol, "market", order_side, order_amount, None, params)
    except Exception as exc:
        msg = str(exc).lower()
        if ("one-way mode" not in msg) and ("positionside" not in msg) and ("position side" not in msg):
            return {"ok": False, "error": str(exc), "qty": expected_base_qty, "order_amount": order_amount, "ccxt_symbol": ccxt_symbol}
        retry_params = {"clientOrderId": client_order_id}
        try:
            od = client.ex.create_order(ccxt_symbol, "market", order_side, order_amount, None, retry_params)
            params = retry_params
        except Exception as retry_exc:
            return {"ok": False, "error": str(retry_exc), "qty": expected_base_qty, "order_amount": order_amount, "ccxt_symbol": ccxt_symbol}
    ex_order_id = order_id_from_response(od)
    fill_px, fill_dt, fetched_order = _fetch_order_fill(client, ccxt_symbol, ex_order_id, wait_sec=args.order_sync_wait_sec, poll_sec=args.order_sync_poll_sec)
    ex_pos = live_fetch_exchange_position(args, client, ccxt_symbol, side) if str(args.live_exchange or "").lower() == "htx" else None
    if fill_px is None and ex_pos and float(ex_pos.get("qty") or 0.0) > 0 and float(ex_pos.get("entry") or 0.0) > 0:
        fill_px = float(ex_pos.get("entry") or 0.0)
        fill_dt = paper.utc_now()
        fetched_order = fetched_order or od
    if fill_px is None:
        return {"ok": False, "error": "open_timeout_no_fill", "qty": expected_base_qty, "order_amount": order_amount, "ccxt_symbol": ccxt_symbol, "order": fetched_order or od, "exchange_order_id": ex_order_id}
    if ex_pos is None:
        ex_pos = live_fetch_exchange_position(args, client, ccxt_symbol, side)
    order = fetched_order or od
    order_qty = live_order_filled_base_qty(args, client, ccxt_symbol, order if isinstance(order, dict) else {}, order_amount)
    return {
        "ok": True,
        "order": order,
        "order_amount": order_amount,
        "contract_size": contract_size,
        "requested_base_qty": base_qty,
        "effective_base_qty": effective_base_qty,
        "configured_min_coin_qty": min_base_qty,
        "configured_min_coin_qty_source": min_base_source,
        "normalized_contract_amount": order_amount,
        "filled_contracts": order_amount,
        "filled_base_qty": order_qty,
        "post_trade_position_qty": float((ex_pos or {}).get("qty") or order_qty),
        "order_qty": order_qty,
        "qty": float((ex_pos or {}).get("qty") or order_qty),
        "entry": float((ex_pos or {}).get("entry") or fill_px),
        "fill_price": float(fill_px),
        "fill_dt": fill_dt.isoformat() if fill_dt else None,
        "params": params,
        "ccxt_symbol": ccxt_symbol,
        "symbol_resolution": symbol_resolution,
        "exchange_order_id": ex_order_id,
        "exchange_position": ex_pos,
    }


def submit_close(args: argparse.Namespace, symbol: str, side: str, qty: float, client_order_id: str) -> Dict[str, Any]:
    client = live_client(args)
    ccxt_symbol, symbol_resolution = resolve_live_trade_symbol(args, client, symbol)
    if not ccxt_symbol:
        return {"ok": False, "error": "live_symbol_unresolved", "symbol_resolution": symbol_resolution}
    ex_before = live_fetch_exchange_position(args, client, ccxt_symbol, side)
    if not ex_before or float(ex_before.get("qty") or 0.0) <= 1e-12:
        return {"ok": True, "synced_only": True, "qty": 0.0, "ccxt_symbol": ccxt_symbol, "reason": "exchange_no_position_before_close"}
    base_qty = min(float(qty or 0.0), float(ex_before.get("qty") or 0.0))
    order_amount, expected_base_qty, contract_size = live_order_amount_from_base_qty(args, client, ccxt_symbol, base_qty, is_close=True, max_base_qty=float(ex_before.get("qty") or 0.0))
    if order_amount <= 0:
        return {"ok": False, "error": "close_qty_zero_after_normalize", "qty": expected_base_qty, "order_amount": order_amount}
    close_side = "sell" if side.upper() == "LONG" else "buy"
    params = live_order_params(client_order_id, side, reduce_only=True, position_mode=args.position_mode, exchange=args.live_exchange)
    try:
        if str(args.live_exchange or "").lower() == "htx":
            od = htx_submit_market_order(client, ccxt_symbol, close_side, order_amount, reduce_only=True)
        else:
            od = client.ex.create_order(ccxt_symbol, "market", close_side, order_amount, None, params)
    except Exception as exc:
        msg = str(exc).lower()
        if ("no position to close" in msg) or ('code":101205' in msg) or ("code': 101205" in msg):
            return {"ok": True, "synced_only": True, "qty": 0.0, "ccxt_symbol": ccxt_symbol, "reason": "exchange_no_position_on_reduce"}
        if ("one-way mode" not in msg) and ("positionside" not in msg) and ("position side" not in msg) and ("reduceonly" not in msg) and ("reduce only" not in msg):
            return {"ok": False, "error": str(exc), "qty": expected_base_qty, "order_amount": order_amount, "ccxt_symbol": ccxt_symbol}
        retry_params: Dict[str, Any] = {"clientOrderId": client_order_id}
        if str(args.position_mode or "").lower() == "hedge":
            retry_params["hedged"] = True
        else:
            retry_params["positionSide"] = side.upper()
        if "reduceonly" not in msg and "reduce only" not in msg:
            retry_params["reduceOnly"] = True
        try:
            od = client.ex.create_order(ccxt_symbol, "market", close_side, order_amount, None, retry_params)
            params = retry_params
        except Exception as retry_exc:
            retry_msg = str(retry_exc).lower()
            if ("no position to close" in retry_msg) or ('code":101205' in retry_msg) or ("code': 101205" in retry_msg):
                return {"ok": True, "synced_only": True, "qty": 0.0, "ccxt_symbol": ccxt_symbol, "reason": "exchange_no_position_on_reduce_retry"}
            return {"ok": False, "error": str(retry_exc), "qty": expected_base_qty, "order_amount": order_amount, "ccxt_symbol": ccxt_symbol}
    ex_order_id = order_id_from_response(od)
    fill_px, fill_dt, fetched_order = _fetch_order_fill(client, ccxt_symbol, ex_order_id, wait_sec=args.order_sync_wait_sec, poll_sec=args.order_sync_poll_sec)
    if fill_px is None:
        return {"ok": False, "error": "close_timeout_no_fill", "qty": expected_base_qty, "order_amount": order_amount, "ccxt_symbol": ccxt_symbol, "order": fetched_order or od, "exchange_order_id": ex_order_id}
    ex_after = live_fetch_exchange_position(args, client, ccxt_symbol, side)
    filled_base_qty = live_order_filled_base_qty(args, client, ccxt_symbol, fetched_order or od, order_amount)
    return {
        "ok": True,
        "order": fetched_order or od,
        "qty": filled_base_qty,
        "order_amount": order_amount,
        "contract_size": contract_size,
        "requested_base_qty": base_qty,
        "normalized_contract_amount": order_amount,
        "filled_contracts": order_amount,
        "filled_base_qty": filled_base_qty,
        "post_trade_position_qty": float((ex_after or {}).get("qty") or 0.0),
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
    lead_page_meta = paper.fetch_lead_margin_balance(session, args.portfolio_id, args.timeout_sec)
    state["last_lead_page_status"] = lead_page_meta
    paper.annotate_positions_with_lead_margin_metadata(positions, lead_page_meta)
    mark_interval = float(getattr(args, "mark_poll_interval_sec", DEFAULT_MARK_POLL_INTERVAL_SEC) or 0.0)
    open_trades = state.get("open_trades") if isinstance(state.get("open_trades"), dict) else {}
    filtered_positions, _ = copy_signal_meta.filter_source_positions(positions, symbol=args.symbol, long_only=bool(args.long_only))
    target_symbols = {
        str(pos.get("symbol") or "").upper().strip()
        for pos in filtered_positions.values()
        if str(pos.get("symbol") or "").strip()
    }
    target_symbols.update(
        str(trade.get("symbol") or "").upper().strip()
        for trade in open_trades.values()
        if isinstance(trade, dict) and str(trade.get("symbol") or "").strip()
    )
    if not live_multi_symbol_mode(args) and str(args.symbol or "").strip():
        target_symbols.add(str(args.symbol).upper().strip())
    has_source_or_local_position = bool(filtered_positions) or bool(open_trades)
    last_mark_ts = float(state.get("last_mark_poll_ts") or 0.0)
    mark_due = has_source_or_local_position or mark_interval <= 0 or not state.get("last_mark_poll_ts") or (now.timestamp() - last_mark_ts) >= mark_interval
    cached_symbol_marks = state.get("cached_symbol_marks") if isinstance(state.get("cached_symbol_marks"), dict) else {}
    cached_symbol_mark_meta = state.get("cached_symbol_mark_meta") if isinstance(state.get("cached_symbol_mark_meta"), dict) else {}
    if mark_due:
        symbol_marks: Dict[str, float] = {}
        symbol_mark_meta: Dict[str, Any] = {}
        errors: Dict[str, str] = {}
        for symbol in sorted(target_symbols):
            try:
                symbol_mark, one_meta = paper.fetch_mark(session, symbol, args.timeout_sec)
                if symbol_mark is not None:
                    symbol_marks[symbol] = float(symbol_mark)
                symbol_mark_meta[symbol] = one_meta
            except Exception as exc:
                errors[symbol] = str(exc)
                if symbol in cached_symbol_marks:
                    try:
                        symbol_marks[symbol] = float(cached_symbol_marks[symbol])
                    except Exception:
                        pass
                    symbol_mark_meta[symbol] = cached_symbol_mark_meta.get(symbol, {"cached": True})
                elif not live_multi_symbol_mode(args) and state.get("cached_mark") is not None:
                    try:
                        symbol_marks[symbol] = float(state["cached_mark"])
                    except Exception:
                        pass
                    symbol_mark_meta[symbol] = {"cached": True, "source": "legacy_cached_mark"}
        state["cached_symbol_marks"] = symbol_marks
        state["cached_symbol_mark_meta"] = symbol_mark_meta
        state["last_mark_poll_ts"] = now.timestamp()
        state["last_mark_poll_utc"] = paper.iso(now)
        if live_multi_symbol_mode(args):
            mark = None
        else:
            mark = symbol_marks.get(str(args.symbol or "").upper().strip())
        state["cached_mark"] = mark
        mark_meta = {
            "cached_mark": mark,
            "mark_poll_interval_sec": mark_interval,
            "mark_poll_due": True,
            "target_symbols": sorted(target_symbols),
            "symbol_marks": symbol_marks,
            "symbol_mark_meta": symbol_mark_meta,
            "errors": errors,
        }
    else:
        mark = state.get("cached_mark")
        mark_meta = {
            "skipped": True,
            "cached_mark": mark,
            "last_mark_poll_utc": state.get("last_mark_poll_utc"),
            "mark_poll_interval_sec": mark_interval,
            "mark_poll_due": False,
            "target_symbols": sorted(target_symbols),
            "symbol_marks": cached_symbol_marks,
            "symbol_mark_meta": cached_symbol_mark_meta,
        }
    args._symbol_marks = dict(mark_meta.get("symbol_marks") or cached_symbol_marks or {})

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
    return positions, history, mark, {"positions": positions_meta, "history": history_meta, "market": mark_meta, "lead_page": lead_page_meta}


def sleep_until_next_poll(interval_sec: float) -> None:
    interval = max(float(interval_sec or DEFAULT_COPY_POLL_INTERVAL_SEC), 1.0)
    now = time.time()
    delay = interval - (now % interval)
    if delay < 0.05:
        delay += interval
    time.sleep(delay)


def sync_trade_from_exchange(args: argparse.Namespace, state: Dict[str, Any], trade: Dict[str, Any]) -> Dict[str, Any]:
    client = live_client(args)
    ccxt_symbol, symbol_resolution = resolve_live_trade_symbol(args, client, trade.get("symbol") or "")
    if not ccxt_symbol:
        return {"synced": False, "reason": "live_symbol_unresolved", "symbol_resolution": symbol_resolution}
    ex_pos = live_fetch_exchange_position(args, client, ccxt_symbol, str(trade.get("side") or "LONG"))
    if not ex_pos:
        return {"synced": False, "reason": "exchange_position_missing", "ccxt_symbol": ccxt_symbol}
    qty = float(ex_pos.get("qty") or 0.0)
    entry = float(ex_pos.get("entry") or 0.0)
    if qty > 0 and entry > 0:
        trade["qty"] = qty
        trade["avg_entry"] = entry
        trade["notional"] = qty * entry
        trade["exchange_position"] = ex_pos
        leverage_policy = effective_source_leverage(args, trade)
        leverage_setup: Dict[str, Any] = {"ok": True, "policy": leverage_policy, "skipped": True}
        if leverage_policy.get("required") and leverage_policy.get("effective_leverage") is not None:
            leverage_setup = ensure_symbol_leverage(
                args,
                state,
                symbol=str(trade.get("symbol") or args.symbol),
                side=str(trade.get("side") or "LONG"),
                margin_mode=str(leverage_policy.get("source_margin_mode") or ""),
                leverage=float(leverage_policy["effective_leverage"]),
                now=paper.utc_now(),
            )
            leverage_setup["policy"] = leverage_policy
        trade["leverage_setup"] = leverage_setup
        upsert_session_position(args, trade, status="OPEN", now=paper.utc_now(), exchange_order_id=str(trade.get("exchange_order_id") or ""))
        return {"synced": True, "qty": qty, "entry": entry, "notional": trade["notional"], "ccxt_symbol": ccxt_symbol, "leverage_setup": leverage_setup}
    return {"synced": False, "reason": "exchange_position_zero", "ccxt_symbol": ccxt_symbol}


def source_size_sync_enabled(args: argparse.Namespace) -> bool:
    return str(getattr(args, "source_size_sync_mode", "off") or "off").strip().lower() == "ratio"


def source_size_snapshot(pos: Dict[str, Any]) -> Dict[str, Any]:
    amount_abs = 0.0
    notional_abs = 0.0
    try:
        amount_abs = abs(float(pos.get("position_amount") or 0.0))
    except Exception:
        amount_abs = 0.0
    try:
        notional_abs = abs(float(pos.get("notional_value") or 0.0))
    except Exception:
        notional_abs = 0.0
    measure = amount_abs if amount_abs > 0 else notional_abs
    measure_field = "position_amount_abs" if amount_abs > 0 else "notional_value_abs"
    return {
        "source_position_amount_abs": amount_abs,
        "source_notional_value_abs": notional_abs,
        "source_size_measure": measure,
        "source_size_measure_field": measure_field,
        "lead_margin_balance_usdt": pos.get("lead_margin_balance_usdt"),
        "source_position_margin_usdt": pos.get("source_position_margin_usdt"),
        "source_position_margin_source": pos.get("source_position_margin_source"),
        "source_margin_fraction": pos.get("source_margin_fraction"),
        "source_margin_fraction_reason": pos.get("source_margin_fraction_reason"),
    }


def _positive_float(raw: Any) -> Optional[float]:
    try:
        value = float(raw)
    except Exception:
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def source_box_target_notional(
    args: argparse.Namespace,
    trade: Dict[str, Any],
    snapshot: Optional[Dict[str, Any]] = None,
    *,
    ratio_fallback: Optional[float] = None,
) -> Tuple[Optional[float], Dict[str, Any]]:
    data = snapshot if isinstance(snapshot, dict) else trade
    margin_fraction = _positive_float(data.get("source_margin_fraction"))
    margin_pool = _positive_float(getattr(args, "initial_equity", None))
    leverage_policy = effective_source_leverage(args, trade)
    effective_leverage = _positive_float(leverage_policy.get("effective_leverage")) or 1.0
    cap = _positive_float(getattr(args, "max_gross_notional_usdt", None))
    if margin_fraction is not None and margin_pool is not None:
        source_box_margin = margin_pool * margin_fraction
        target = source_box_margin * effective_leverage
        capped_target = min(target, cap) if cap is not None else target
        return capped_target, {
            "basis": "source_margin_fraction_x_follower_margin_pool_x_effective_leverage",
            "source_margin_fraction": margin_fraction,
            "follower_margin_pool_usdt": margin_pool,
            "source_box_margin_usdt": source_box_margin,
            "effective_leverage": effective_leverage,
            "uncapped_target_notional": target,
            "cap_notional": cap,
            "leverage_policy": leverage_policy,
        }
    ratio = _positive_float(ratio_fallback)
    old_target = _positive_float(trade.get("target_notional"))
    if ratio is not None and old_target is not None:
        target = old_target * ratio
        capped_target = min(target, cap) if cap is not None else target
        return capped_target, {
            "basis": "source_position_amount_ratio_x_previous_box",
            "ratio": ratio,
            "previous_target_notional": old_target,
            "uncapped_target_notional": target,
            "capped": capped_target < target - 1e-9,
            "cap_notional": cap,
            "leverage_policy": leverage_policy,
        }
    return None, {
        "basis": "unavailable",
        "reason": "missing_source_margin_fraction_and_ratio",
        "source_margin_fraction": data.get("source_margin_fraction"),
        "ratio": ratio_fallback,
        "leverage_policy": leverage_policy,
    }


def resize_trade_source_box(
    trade: Dict[str, Any],
    snapshot: Optional[Dict[str, Any]],
    now: datetime,
    args: argparse.Namespace,
    *,
    reason: str,
    ratio_fallback: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    target, meta = source_box_target_notional(args, trade, snapshot, ratio_fallback=ratio_fallback)
    if target is None:
        trade["source_box_target_basis"] = meta.get("basis")
        trade["source_box_last_resize_skip"] = meta
        return None
    event = copy_signal_meta.dca.resize_trade_plan(
        trade,
        target,
        now=now,
        iso_fn=paper.iso,
        reason=reason,
        basis=str(meta.get("basis") or "source_box"),
        sizing=trade.get("strategy_sizing") if isinstance(trade.get("strategy_sizing"), dict) else None,
    )
    trade["source_box_target_meta"] = meta
    if event is not None:
        event["source_box"] = meta
    return event


def source_box_guard_args(state: Dict[str, Any], trade: Dict[str, Any], args: argparse.Namespace) -> Tuple[argparse.Namespace, Dict[str, Any]]:
    open_trades = state.get("open_trades") if isinstance(state.get("open_trades"), dict) else {}
    box_total = 0.0
    side_totals: Dict[str, float] = {}
    current_key = str(trade.get("key") or "")
    has_current_box = _positive_float(trade.get("source_box_current_target_notional")) is not None
    for key, item in open_trades.items():
        target = _positive_float(item.get("source_box_current_target_notional"))
        if target is None:
            continue
        box_total += target
        side = str(item.get("side") or "").upper()
        side_totals[side] = side_totals.get(side, 0.0) + target
    if current_key not in open_trades and has_current_box:
        target = float(trade.get("source_box_current_target_notional") or 0.0)
        box_total += target
        side = str(trade.get("side") or "").upper()
        side_totals[side] = side_totals.get(side, 0.0) + target
    if box_total <= 0:
        return args, {"enabled": False, "reason": "no_source_box_targets"}
    static_gross = float(getattr(args, "max_gross_notional_usdt", 0.0) or 0.0)
    static_side = float(getattr(args, "max_one_side_notional_usdt", 0.0) or 0.0)
    side = str(trade.get("side") or "").upper()
    dynamic_gross = max(static_gross, box_total * SOURCE_BOX_GUARD_HEADROOM)
    dynamic_side = max(static_side, side_totals.get(side, 0.0) * SOURCE_BOX_GUARD_HEADROOM)
    if dynamic_gross <= static_gross + 1e-9 and dynamic_side <= static_side + 1e-9:
        return args, {
            "enabled": True,
            "changed": False,
            "box_total_notional": box_total,
            "box_side_notional": side_totals.get(side, 0.0),
            "headroom": SOURCE_BOX_GUARD_HEADROOM,
            "max_gross_notional_usdt": static_gross,
            "max_one_side_notional_usdt": static_side,
        }
    guard_args = argparse.Namespace(**vars(args))
    guard_args.max_gross_notional_usdt = dynamic_gross
    guard_args.max_one_side_notional_usdt = dynamic_side
    return guard_args, {
        "enabled": True,
        "changed": True,
        "box_total_notional": box_total,
        "box_side_notional": side_totals.get(side, 0.0),
        "headroom": SOURCE_BOX_GUARD_HEADROOM,
        "static_max_gross_notional_usdt": static_gross,
        "static_max_one_side_notional_usdt": static_side,
        "dynamic_max_gross_notional_usdt": dynamic_gross,
        "dynamic_max_one_side_notional_usdt": dynamic_side,
    }


def _open_notional_headroom(state: Dict[str, Any], trade: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    open_trades = state.get("open_trades") if isinstance(state.get("open_trades"), dict) else {}
    side = str(trade.get("side") or "").upper()
    gross = 0.0
    side_total = 0.0
    for item in open_trades.values():
        try:
            notional = abs(float(item.get("notional") or 0.0))
        except Exception:
            notional = 0.0
        gross += notional
        if str(item.get("side") or "").upper() == side:
            side_total += notional
    static_gross = _positive_float(getattr(args, "max_gross_notional_usdt", None))
    static_side = _positive_float(getattr(args, "max_one_side_notional_usdt", None))
    gross_headroom = math.inf if static_gross is None else max(static_gross - gross, 0.0)
    side_headroom = math.inf if static_side is None else max(static_side - side_total, 0.0)
    return {
        "gross_open_notional": gross,
        "one_side_open_notional": side_total,
        "gross_headroom": gross_headroom,
        "one_side_headroom": side_headroom,
        "max_gross_notional_usdt": static_gross,
        "max_one_side_notional_usdt": static_side,
    }


def _source_box_trade_headroom(trade: Dict[str, Any]) -> Dict[str, Any]:
    target = _positive_float(trade.get("source_box_current_target_notional")) or _positive_float(trade.get("target_notional"))
    try:
        current_notional = abs(float(trade.get("notional") or 0.0))
    except Exception:
        current_notional = 0.0
    if target is None:
        headroom = math.inf
    else:
        headroom = max((target * SOURCE_BOX_GUARD_HEADROOM) - current_notional, 0.0)
    return {
        "source_box_target_notional": target,
        "source_box_headroom": headroom,
        "source_box_headroom_multiplier": SOURCE_BOX_GUARD_HEADROOM,
        "current_trade_notional": current_notional,
    }


def _source_size_clamp_min_order_notional(
    trade: Dict[str, Any],
    args: argparse.Namespace,
    *,
    expected_price: float,
    min_adjust_notional: float,
) -> float:
    minimum = max(float(min_adjust_notional or 0.0), 0.0)
    trade_min = _positive_float(trade.get("min_order_notional"))
    if trade_min is not None:
        minimum = max(minimum, trade_min)
    client = getattr(args, "_live_client", None)
    if client is not None and expected_price > 0:
        ccxt_symbol, _resolution = resolve_live_trade_symbol(args, client, str(trade.get("symbol") or ""))
        if ccxt_symbol:
            min_base_qty, _source = configured_min_base_qty(args, str(trade.get("symbol") or ""), ccxt_symbol)
            if min_base_qty > 0:
                minimum = max(minimum, min_base_qty * expected_price)
    return minimum


def _source_size_clamp_event_allowed(trade: Dict[str, Any], now: datetime, reason: str) -> bool:
    last = trade.get("source_size_increase_clamp_last")
    throttle = DEFAULT_SOURCE_SIZE_CLAMP_TELEMETRY_SEC
    if isinstance(last, dict):
        last_reason = str(last.get("reason") or "")
        last_utc = str(last.get("utc") or "")
        try:
            last_dt = datetime.fromisoformat(last_utc.replace("Z", "+00:00"))
        except Exception:
            last_dt = None
        if last_reason == reason and last_dt is not None and (now - last_dt).total_seconds() < throttle:
            return False
    trade["source_size_increase_clamp_last"] = {"utc": paper.iso(now), "reason": reason}
    return True


def clamp_source_size_increase_notional(
    state: Dict[str, Any],
    trade: Dict[str, Any],
    *,
    requested_notional: float,
    expected_price: float,
    min_adjust_notional: float,
    now: datetime,
    args: argparse.Namespace,
) -> Tuple[float, Optional[Dict[str, Any]]]:
    requested = max(float(requested_notional or 0.0), 0.0)
    cap_headroom = _open_notional_headroom(state, trade, args)
    box_headroom = _source_box_trade_headroom(trade)
    allowed_headroom = min(
        requested,
        float(cap_headroom["gross_headroom"]),
        float(cap_headroom["one_side_headroom"]),
        float(box_headroom["source_box_headroom"]),
    )
    allowed = max(allowed_headroom, 0.0)
    min_order_notional = _source_size_clamp_min_order_notional(
        trade,
        args,
        expected_price=expected_price,
        min_adjust_notional=min_adjust_notional,
    )
    preflight: Optional[Dict[str, Any]] = None
    effective_order_notional = allowed
    if allowed > 0:
        preflight = live_open_order_preflight(args, str(trade.get("symbol") or ""), expected_price, allowed)
        effective_order_notional = max(allowed, float(preflight.get("normalized_notional") or 0.0))
    reason = ""
    if allowed <= 1e-9:
        reason = "source_size_increase_no_headroom"
    elif min_order_notional > 0 and allowed + 1e-9 < min_order_notional:
        reason = "source_size_increase_headroom_below_min_order"
    elif effective_order_notional > allowed + 1e-9:
        reason = "source_size_increase_normalized_order_exceeds_headroom"
    elif allowed < requested - 1e-9:
        reason = "source_size_increase_clamped_to_headroom"

    if not reason:
        return allowed, None

    event = {
        "type": "source_size_increase_clamped",
        "key": trade.get("key"),
        "utc": paper.iso(now),
        "fill_type": "source_size_increase",
        "reason": reason,
        "requested_notional": requested,
        "clamped_notional": allowed if reason == "source_size_increase_clamped_to_headroom" else 0.0,
        "effective_order_notional": effective_order_notional,
        "min_order_notional": min_order_notional,
        "cap_headroom": cap_headroom,
        "source_box_headroom": box_headroom,
    }
    if preflight is not None:
        event["exchange_preflight"] = preflight
    if not _source_size_clamp_event_allowed(trade, now, reason):
        event["telemetry_suppressed"] = True
    if reason != "source_size_increase_clamped_to_headroom":
        return 0.0, event
    return allowed, event


def update_trade_source_size_snapshot(trade: Dict[str, Any], snapshot: Dict[str, Any], now: datetime) -> None:
    trade["source_position_amount_abs"] = float(snapshot.get("source_position_amount_abs") or 0.0)
    trade["source_notional_value_abs"] = float(snapshot.get("source_notional_value_abs") or 0.0)
    trade["source_size_measure"] = float(snapshot.get("source_size_measure") or 0.0)
    trade["source_size_measure_field"] = str(snapshot.get("source_size_measure_field") or "")
    for field in (
        "lead_margin_balance_usdt",
        "source_position_margin_usdt",
        "source_position_margin_source",
        "source_margin_fraction",
        "source_margin_fraction_reason",
    ):
        if field in snapshot:
            trade[field] = snapshot.get(field)
    trade["source_size_last_sync_utc"] = paper.iso(now)


def source_size_sync_due(trade: Dict[str, Any], now: datetime, args: argparse.Namespace) -> bool:
    interval = float(getattr(args, "source_size_sync_interval_sec", DEFAULT_SOURCE_SIZE_SYNC_INTERVAL_SEC) or 0.0)
    if interval <= 0:
        return True
    last = copy_signal_meta.parse_iso_dt(trade.get("source_size_last_checked_utc"))
    if not last:
        return True
    return (now - last).total_seconds() >= max(interval - 0.5, 0.0)


def source_size_observation_event(key: str, trade: Dict[str, Any], snapshot: Dict[str, Any], now: datetime, *, changed: bool, ratio: Optional[float] = None) -> Dict[str, Any]:
    return {
        "type": "source_size_observed",
        "key": key,
        "utc": paper.iso(now),
        "symbol": trade.get("symbol"),
        "side": trade.get("side"),
        "changed": bool(changed),
        "ratio": ratio,
        "source_position_amount_abs": snapshot.get("source_position_amount_abs"),
        "source_notional_value_abs": snapshot.get("source_notional_value_abs"),
        "source_size_measure": snapshot.get("source_size_measure"),
        "source_size_measure_field": snapshot.get("source_size_measure_field"),
        "lead_margin_balance_usdt": snapshot.get("lead_margin_balance_usdt"),
        "source_position_margin_usdt": snapshot.get("source_position_margin_usdt"),
        "source_position_margin_source": snapshot.get("source_position_margin_source"),
        "source_margin_fraction": snapshot.get("source_margin_fraction"),
        "source_margin_fraction_reason": snapshot.get("source_margin_fraction_reason"),
        "previous_source_size_measure": trade.get("source_size_measure"),
        "follower_notional": trade.get("notional"),
        "follower_qty": trade.get("qty"),
    }


def source_margin_increase_meta(trade: Dict[str, Any], snapshot: Dict[str, Any], *, min_change_pct: float = 0.0) -> Optional[Dict[str, Any]]:
    previous_margin = _positive_float(trade.get("source_position_margin_usdt"))
    current_margin = _positive_float(snapshot.get("source_position_margin_usdt"))
    previous_fraction = _positive_float(trade.get("source_margin_fraction"))
    current_fraction = _positive_float(snapshot.get("source_margin_fraction"))
    if current_margin is None or previous_margin is None:
        return None
    delta_margin = current_margin - previous_margin
    if delta_margin <= 1e-9:
        return None
    change_pct = (delta_margin / max(previous_margin, 1e-12)) * 100.0
    if change_pct + 1e-9 < max(float(min_change_pct or 0.0), 0.0):
        return None
    delta_fraction = None
    if current_fraction is not None and previous_fraction is not None:
        delta_fraction = current_fraction - previous_fraction
    return {
        "previous_source_position_margin_usdt": previous_margin,
        "source_position_margin_usdt": current_margin,
        "source_margin_delta_usdt": delta_margin,
        "source_margin_change_pct": change_pct,
        "previous_source_margin_fraction": previous_fraction,
        "source_margin_fraction": current_fraction,
        "source_margin_fraction_delta": delta_fraction,
        "source_position_margin_source": snapshot.get("source_position_margin_source"),
        "source_margin_fraction_reason": snapshot.get("source_margin_fraction_reason"),
    }


def source_margin_add_not_followed_event(
    key: str,
    trade: Dict[str, Any],
    snapshot: Dict[str, Any],
    now: datetime,
    *,
    mark: Optional[float],
    reason: str,
    margin_meta: Dict[str, Any],
) -> Dict[str, Any]:
    price = None
    try:
        price = copy_signal_meta.mark_for_symbol(None, mark, trade.get("symbol")) if mark is not None else None
    except Exception:
        price = None
    if price is None:
        price = trade.get("last_mark") or trade.get("avg_entry") or trade.get("lead_entry_price")
    return {
        "type": "source_margin_add_not_followed",
        "key": key,
        "utc": paper.iso(now),
        "symbol": trade.get("symbol"),
        "side": trade.get("side"),
        "price": price,
        "reason": reason,
        "action": "source_margin_add_not_followed",
        "text": "Source margin increased; follower did not add isolated margin or notional",
        "source_position_amount_abs": snapshot.get("source_position_amount_abs"),
        "source_notional_value_abs": snapshot.get("source_notional_value_abs"),
        "source_size_measure": snapshot.get("source_size_measure"),
        "follower_notional": trade.get("notional"),
        "follower_qty": trade.get("qty"),
        **margin_meta,
    }


def append_source_margin_add_not_followed(
    state: Dict[str, Any],
    events: List[Dict[str, Any]],
    key: str,
    trade: Dict[str, Any],
    snapshot: Dict[str, Any],
    now: datetime,
    *,
    mark: Optional[float],
    reason: str,
    margin_meta: Optional[Dict[str, Any]],
) -> None:
    if not margin_meta:
        return
    event = source_margin_add_not_followed_event(key, trade, snapshot, now, mark=mark, reason=reason, margin_meta=margin_meta)
    record_source_size_observation(state, event)
    events.append(event)


def record_source_size_observation(state: Dict[str, Any], event: Dict[str, Any]) -> None:
    state.setdefault("source_size_observations", []).append(event)
    state["source_size_observations"] = state["source_size_observations"][-5000:]


def live_reduce_for_source_size(
    state: Dict[str, Any],
    trade: Dict[str, Any],
    *,
    now: datetime,
    expected_price: float,
    reduce_notional: float,
    source_snapshot: Dict[str, Any],
    ratio: float,
    args: argparse.Namespace,
) -> Dict[str, Any]:
    backoff = order_error_backoff_active(state, now)
    if backoff:
        return {
            "type": "live_source_size_reduce_blocked",
            "key": trade.get("key"),
            "reason": str(backoff.get("reason") or "order_error_backoff"),
            "backoff": backoff,
            "requested_reduce_notional": reduce_notional,
        }
    attempt_guard = order_attempt_guard_active(state, now, args)
    if attempt_guard:
        return {
            "type": "live_source_size_reduce_blocked",
            "key": trade.get("key"),
            "reason": str(attempt_guard.get("reason") or "order_attempt_guard"),
            "order_attempt_guard": attempt_guard,
            "requested_reduce_notional": reduce_notional,
        }
    qty = float(trade.get("qty") or 0.0)
    avg_entry = float(trade.get("avg_entry") or expected_price or 0.0)
    if qty <= 0 or avg_entry <= 0:
        return {"type": "live_source_size_reduce_skipped", "key": trade.get("key"), "reason": "trade_qty_or_entry_missing", "requested_reduce_notional": reduce_notional}
    reduce_qty = min(qty, max(float(reduce_notional), 0.0) / max(float(expected_price or avg_entry), 1e-12))
    if reduce_qty <= 0:
        return {"type": "live_source_size_reduce_skipped", "key": trade.get("key"), "reason": "reduce_qty_zero", "requested_reduce_notional": reduce_notional}
    client_order_id = stable_client_order_id(
        "source-size-reduce",
        args.run_id,
        trade.get("key"),
        trade.get("lead_position_id"),
        trade.get("opened_at_utc"),
        source_snapshot.get("source_size_measure"),
    )
    post_attempt = register_order_post_attempt(state, now, args, action="CLOSE", symbol=str(trade.get("symbol") or args.live_symbol), side=str(trade.get("side") or "LONG"))
    submitted = submit_close(args, str(trade["symbol"]), str(trade["side"]), reduce_qty, client_order_id)
    if not submitted.get("ok"):
        error_text = str(submitted.get("error") or "unknown_order_error")
        backoff_payload = register_order_error_backoff(state, error_text, now, args)
        record_session_order(
            args,
            now=now,
            symbol=str(trade.get("symbol") or args.live_symbol),
            side=str(trade["side"]),
            type_="CLOSE",
            price=expected_price,
            qty=reduce_qty,
            status="REJECTED",
            reason="source_size_reduce_failed",
            exchange_order_id=str(submitted.get("exchange_order_id") or ""),
            extra={**submitted, "order_error_backoff": backoff_payload, "post_attempt": post_attempt, "source_size_sync": source_snapshot},
        )
        return {
            "type": "live_source_size_reduce_failed",
            "key": trade.get("key"),
            "reason": "source_size_reduce_failed",
            "error": error_text,
            "requested_reduce_notional": reduce_notional,
            "backoff": backoff_payload,
            "post_attempt": post_attempt,
        }
    order = safe_order(submitted.get("order"))
    fill_price = float(submitted.get("fill_price") or avg_price(order, expected_price))
    filled_qty = min(float(submitted.get("qty") or reduce_qty), qty)
    exit_fee = order_fee_usdt(submitted.get("order") if isinstance(submitted.get("order"), dict) else order)
    closed_entry_notional = filled_qty * avg_entry
    old_fees = float(trade.get("fees_paid") or 0.0)
    closed_fee_share = old_fees * min(max(filled_qty / max(qty, 1e-12), 0.0), 1.0)
    realized_pnl = paper.ret_for(str(trade["side"]), avg_entry, fill_price) * closed_entry_notional - closed_fee_share - exit_fee
    old_notional = float(trade.get("notional") or closed_entry_notional)
    ex_after = submitted.get("exchange_position_after")
    if isinstance(ex_after, dict) and float(ex_after.get("qty") or 0.0) > 0 and float(ex_after.get("entry") or 0.0) > 0:
        trade["qty"] = float(ex_after.get("qty") or 0.0)
        trade["avg_entry"] = float(ex_after.get("entry") or avg_entry)
        trade["notional"] = trade["qty"] * trade["avg_entry"]
        trade["exchange_position"] = ex_after
    else:
        new_qty = max(qty - filled_qty, 0.0)
        trade["qty"] = new_qty
        trade["avg_entry"] = avg_entry if new_qty > 0 else 0.0
        trade["notional"] = new_qty * avg_entry
        trade["exchange_position"] = ex_after or {}
    trade["fees_paid"] = max(old_fees - closed_fee_share, 0.0)
    state["equity"] = float(state.get("equity") or args.initial_equity) + realized_pnl
    exit_lag = fill_lag_sec(now, submitted.get("fill_dt"))
    exit_slip = signed_slip_bp(str(trade["side"]), expected_price, fill_price, is_close=True)
    fill = {
        "ts_ms": int(now.timestamp() * 1000),
        "utc": paper.iso(now),
        "risk_action": "source_size_reduce",
        "symbol": trade["symbol"],
        "live_symbol": submitted.get("ccxt_symbol"),
        "side": trade["side"],
        "fill_type": "source_size_reduce",
        "reason": "source_position_amount_decreased",
        "expected_price": expected_price,
        "live_fill_price": fill_price,
        "requested_reduce_notional": reduce_notional,
        "closed_entry_notional": closed_entry_notional,
        "old_notional": old_notional,
        "new_notional": trade["notional"],
        "source_size_ratio": ratio,
        "source_size_snapshot": source_snapshot,
        "qty": filled_qty,
        "post_trade_position_qty": submitted.get("post_trade_position_qty"),
        "fee_usdt": exit_fee,
        "closed_open_fee_share_usdt": closed_fee_share,
        "realized_pnl_usdt": realized_pnl,
        "exit_slip_bp": exit_slip,
        "exit_lag_sec": exit_lag,
        "paper_only": False,
        "live_order": order,
        "client_order_id": client_order_id,
        "exchange_order_id": submitted.get("exchange_order_id"),
        "fill_dt": submitted.get("fill_dt"),
        "exchange_position_after": ex_after,
        "post_attempt": post_attempt,
    }
    state.setdefault("paper_orders", []).append(fill)
    state["paper_orders"] = state["paper_orders"][-5000:]
    trade.setdefault("fills", []).append(fill)
    trade["exchange_order_id"] = submitted.get("exchange_order_id")
    record_session_order(
        args,
        now=now,
        symbol=str(trade.get("symbol") or args.live_symbol),
        side=str(trade["side"]),
        type_="CLOSE",
        price=fill_price,
        qty=filled_qty,
        status="FILLED",
        reason="source_size_reduce",
        exchange_order_id=str(submitted.get("exchange_order_id") or ""),
        extra={"fill": fill, "submitted": submitted, "source_size_sync": source_snapshot, "post_attempt": post_attempt},
    )
    upsert_session_position(args, trade, status="OPEN", now=now, exchange_order_id=str(submitted.get("exchange_order_id") or ""))
    clear_order_error_backoff(state)
    return {"type": "live_source_size_reduce", "key": trade.get("key"), "fill": fill}


def apply_source_size_sync(
    state: Dict[str, Any],
    open_trades: Dict[str, Dict[str, Any]],
    current_source: Dict[str, Dict[str, Any]],
    mark: Optional[float],
    now: datetime,
    args: argparse.Namespace,
) -> List[Dict[str, Any]]:
    if not source_size_sync_enabled(args):
        return []
    events: List[Dict[str, Any]] = []
    min_change_pct = max(float(getattr(args, "source_size_sync_min_change_pct", 0.0) or 0.0), 0.0)
    min_adjust_notional = max(float(getattr(args, "source_size_sync_min_adjust_notional_usdt", 0.0) or 0.0), 0.0)
    for key in sorted(set(open_trades) & set(current_source)):
        trade = open_trades[key]
        if not source_size_sync_due(trade, now, args):
            continue
        pos = current_source[key]
        snapshot = source_size_snapshot(pos)
        trade["source_size_last_checked_utc"] = paper.iso(now)
        previous = float(trade.get("source_size_measure") or trade.get("source_position_amount_abs") or trade.get("source_notional_value_abs") or 0.0)
        current = float(snapshot.get("source_size_measure") or 0.0)
        margin_increase = source_margin_increase_meta(trade, snapshot, min_change_pct=min_change_pct)
        if previous <= 0 or current <= 0:
            observation = source_size_observation_event(key, trade, snapshot, now, changed=False, ratio=None)
            observation["reason"] = "source_size_seed_or_missing"
            record_source_size_observation(state, observation)
            events.append(observation)
            resize_event = resize_trade_source_box(trade, snapshot, now, args, reason="source_size_seed_or_missing")
            if resize_event:
                events.append(resize_event)
            append_source_margin_add_not_followed(
                state,
                events,
                key,
                trade,
                snapshot,
                now,
                mark=mark,
                reason="source_size_seed_or_missing",
                margin_meta=margin_increase,
            )
            update_trade_source_size_snapshot(trade, snapshot, now)
            continue
        ratio = current / previous
        change_pct = abs(ratio - 1.0) * 100.0
        changed = change_pct > 0.0
        observation = source_size_observation_event(key, trade, snapshot, now, changed=changed, ratio=ratio)
        observation["change_pct"] = change_pct
        record_source_size_observation(state, observation)
        events.append(observation)
        resize_event = resize_trade_source_box(trade, snapshot, now, args, reason="source_size_sync", ratio_fallback=ratio)
        if resize_event:
            events.append(resize_event)
        if not changed or change_pct < min_change_pct:
            if changed:
                observation["action"] = "source_size_change_below_min_change_pct"
            append_source_margin_add_not_followed(
                state,
                events,
                key,
                trade,
                snapshot,
                now,
                mark=mark,
                reason="source_position_margin_increased_without_followed_size_change",
                margin_meta=margin_increase,
            )
            update_trade_source_size_snapshot(trade, snapshot, now)
            continue
        current_notional = float(trade.get("notional") or 0.0)
        desired_notional = current_notional * ratio
        delta_notional = desired_notional - current_notional
        abs_delta = abs(delta_notional)
        observation["desired_follower_notional"] = desired_notional
        observation["delta_notional"] = delta_notional
        if abs_delta < min_adjust_notional:
            observation["action"] = "source_size_change_below_min_adjust_notional"
            append_source_margin_add_not_followed(
                state,
                events,
                key,
                trade,
                snapshot,
                now,
                mark=mark,
                reason="source_margin_increase_below_min_adjust_notional",
                margin_meta=margin_increase,
            )
            update_trade_source_size_snapshot(trade, snapshot, now)
            continue
        expected_price = float(copy_signal_meta.mark_for_symbol(args, mark, trade.get("symbol")) or trade.get("last_mark") or trade.get("avg_entry") or trade.get("lead_entry_price") or 0.0)
        if expected_price <= 0:
            observation["action"] = "source_size_sync_skipped_missing_price"
            append_source_margin_add_not_followed(
                state,
                events,
                key,
                trade,
                snapshot,
                now,
                mark=mark,
                reason="source_margin_increase_missing_price",
                margin_meta=margin_increase,
            )
            continue
        if delta_notional > 0:
            clamped_notional, clamp_event = clamp_source_size_increase_notional(
                state,
                trade,
                requested_notional=delta_notional,
                expected_price=expected_price,
                min_adjust_notional=min_adjust_notional,
                now=now,
                args=args,
            )
            if clamp_event is not None:
                clamp_event["source_size_sync"] = {
                    **snapshot,
                    "previous_measure": previous,
                    "ratio": ratio,
                    "delta_notional": delta_notional,
                    "requested_delta_notional": delta_notional,
                    "clamped_delta_notional": clamped_notional,
                }
                observation["action"] = str(clamp_event.get("reason") or "source_size_increase_clamped")
                if not clamp_event.get("telemetry_suppressed"):
                    record_source_size_observation(state, clamp_event)
                    events.append(clamp_event)
            if clamped_notional <= 0:
                if clamp_event is not None and not clamp_event.get("telemetry_suppressed"):
                    append_source_margin_add_not_followed(
                        state,
                        events,
                        key,
                        trade,
                        snapshot,
                        now,
                        mark=mark,
                        reason=str(clamp_event.get("reason") or "source_size_increase_clamped"),
                        margin_meta=margin_increase,
                    )
                continue
            event = live_add_fill(
                state,
                trade,
                now=now,
                expected_price=expected_price,
                notional=clamped_notional,
                fill_type="source_size_increase",
                reason="source_position_amount_increased",
                mark=mark,
                args=args,
            )
            event["source_size_sync"] = {
                **snapshot,
                "previous_measure": previous,
                "ratio": ratio,
                "delta_notional": clamped_notional,
                "requested_delta_notional": delta_notional,
                "clamped_delta_notional": clamped_notional,
            }
            if event.get("type") == "live_fill" and clamped_notional >= delta_notional - 1e-9:
                update_trade_source_size_snapshot(trade, snapshot, now)
            else:
                append_source_margin_add_not_followed(
                    state,
                    events,
                    key,
                    trade,
                    snapshot,
                    now,
                    mark=mark,
                    reason=str(event.get("reason") or event.get("type") or "source_size_increase_not_filled"),
                    margin_meta=margin_increase,
                )
            events.append(event)
            continue
        append_source_margin_add_not_followed(
            state,
            events,
            key,
            trade,
            snapshot,
            now,
            mark=mark,
            reason="source_margin_increased_while_source_size_ratio_requested_reduce",
            margin_meta=margin_increase,
        )
        event = live_reduce_for_source_size(
            state,
            trade,
            now=now,
            expected_price=expected_price,
            reduce_notional=abs_delta,
            source_snapshot=snapshot,
            ratio=ratio,
            args=args,
        )
        event["source_size_sync"] = {**snapshot, "previous_measure": previous, "ratio": ratio, "delta_notional": delta_notional}
        if event.get("type") == "live_source_size_reduce":
            update_trade_source_size_snapshot(trade, snapshot, now)
        events.append(event)
    return events


def seed_open_trades_from_exchange(
    state: Dict[str, Any],
    positions: List[Dict[str, Any]],
    mark: Optional[float],
    now: datetime,
    args: argparse.Namespace,
) -> List[Dict[str, Any]]:
    open_trades: Dict[str, Dict[str, Any]] = state.setdefault("open_trades", {})
    current, signal_events = copy_signal_meta.filter_source_positions(positions, symbol=args.symbol, long_only=bool(args.long_only))
    events: List[Dict[str, Any]] = list(signal_events)
    if not current:
        return events
    client = live_client(args)
    for key, pos in sorted(current.items()):
        if key in open_trades:
            continue
        side = str(pos.get("side") or "").upper()
        ccxt_symbol, symbol_resolution = resolve_live_trade_symbol(args, client, str(pos.get("symbol") or ""))
        if not ccxt_symbol:
            events.append({"type": "exchange_position_seed_skipped", "key": key, "reason": "live_symbol_unresolved", "symbol_resolution": symbol_resolution})
            continue
        ex_pos = live_fetch_exchange_position(args, client, ccxt_symbol, side)
        qty = float((ex_pos or {}).get("qty") or 0.0)
        entry = float((ex_pos or {}).get("entry") or 0.0)
        if qty <= 0 or entry <= 0:
            continue
        strategy_sizing, strategy_source = copy_signal_meta.strategy_sizing_for_symbol(args, pos.get("symbol"))
        if strategy_sizing:
            pos["strategy_sizing"] = strategy_sizing
            pos["strategy_config_source"] = strategy_source
        plan = copy_signal_meta.dca.build_plan(
            float(state.get("equity") or args.initial_equity),
            args,
            float(pos["entry_price"]),
            side=str(pos.get("side") or "LONG"),
            sizing=strategy_sizing,
        )
        pos_mark = copy_signal_meta.mark_for_symbol(args, mark, pos.get("symbol"))
        trade = copy_signal_meta.dca.build_trade_from_source(pos, plan, now=now, mark=pos_mark, iso_fn=paper.iso)
        resize_event = resize_trade_source_box(trade, source_size_snapshot(pos), now, args, reason="exchange_seed_source_box")
        trade["qty"] = qty
        trade["notional"] = qty * entry
        trade["avg_entry"] = entry
        trade["exchange_position"] = ex_pos
        trade["seeded_from_exchange"] = True
        leverage_policy = effective_source_leverage(args, trade)
        leverage_setup: Dict[str, Any] = {"ok": True, "policy": leverage_policy, "skipped": True}
        if leverage_policy.get("required") and leverage_policy.get("effective_leverage") is not None:
            leverage_setup = ensure_symbol_leverage(
                args,
                state,
                symbol=trade["symbol"],
                side=str(trade["side"]),
                margin_mode=str(leverage_policy.get("source_margin_mode") or ""),
                leverage=float(leverage_policy["effective_leverage"]),
                now=now,
            )
            leverage_setup["policy"] = leverage_policy
        open_trades[key] = trade
        upsert_session_position(args, trade, status="OPEN", now=now, exchange_order_id=str(trade.get("exchange_order_id") or ""))
        if resize_event:
            events.append(resize_event)
        events.append({"type": "exchange_position_seeded", "key": key, "qty": qty, "entry": entry, "notional": trade["notional"], "ccxt_symbol": ccxt_symbol, "leverage_setup": leverage_setup})
    return events


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
    protection = getattr(args, "_last_protection", {})
    if isinstance(protection, dict) and protection.get("block_new_entries"):
        return {
            "type": "live_entry_blocked",
            "key": trade.get("key"),
            "fill_type": fill_type,
            "reason": "protection_block_new_entries",
            "protection": protection,
            "requested_notional": notional,
        }
    backoff = order_error_backoff_active(state, now)
    if backoff:
        return {
            "type": "live_entry_blocked",
            "key": trade.get("key"),
            "fill_type": fill_type,
            "reason": str(backoff.get("reason") or "order_error_backoff"),
            "backoff": backoff,
            "requested_notional": notional,
        }
    attempt_key = entry_attempt_key(trade, fill_type)
    failure = entry_failure_active(state, attempt_key, now)
    if failure:
        return {
            "type": "live_entry_blocked",
            "key": trade.get("key"),
            "fill_type": fill_type,
            "reason": "entry_failure_cooldown",
            "attempt_key": attempt_key,
            "failure": failure,
            "requested_notional": notional,
        }
    preflight = live_open_order_preflight(args, trade["symbol"], expected_price, notional)
    if not preflight.get("ok", True):
        return {
            "type": "live_entry_blocked",
            "key": trade.get("key"),
            "fill_type": fill_type,
            "reason": str(preflight.get("reason") or "exchange_open_preflight_failed"),
            "exchange_preflight": preflight,
            "requested_notional": notional,
        }
    order_notional = max(float(notional or 0.0), float(preflight.get("normalized_notional") or 0.0))
    guard_args, source_box_guard = source_box_guard_args(state, trade, args)
    ok, guard_reason, guard_detail = paper.guard_new_entry(
        state, side=str(trade["side"]), add_notional=order_notional, mark=mark, now=now, args=guard_args
    )
    guard_detail["source_box_guard"] = source_box_guard
    if not ok:
        return {
            "type": "live_entry_blocked",
            "key": trade.get("key"),
            "fill_type": fill_type,
            "reason": guard_reason,
            "guard": guard_detail,
            "exchange_preflight": preflight,
            "requested_notional": notional,
            "effective_order_notional": order_notional,
        }
    leverage_policy = effective_source_leverage(args, trade)
    leverage_setup: Dict[str, Any] = {"ok": True, "policy": leverage_policy, "skipped": True}
    if leverage_policy.get("required"):
        effective_lev = leverage_policy.get("effective_leverage")
        if effective_lev is None:
            error_text = str(leverage_policy.get("error") or "missing_source_leverage")
            failure_payload = register_entry_failure(state, attempt_key, error_text, now, args)
            state["last_leverage_setup"] = {"ok": False, "error": error_text, "policy": leverage_policy, "utc": paper.iso(now)}
            return {
                "type": "live_entry_blocked",
                "key": trade.get("key"),
                "fill_type": fill_type,
                "reason": "leverage_setup_failed",
                "error": error_text,
                "leverage": leverage_policy,
                "exchange_preflight": preflight,
                "requested_notional": notional,
                "effective_order_notional": order_notional,
                "attempt_key": attempt_key,
                "entry_failure": failure_payload,
            }
        leverage_setup = ensure_symbol_leverage(
            args,
            state,
            symbol=trade["symbol"],
            side=str(trade["side"]),
            margin_mode=str(leverage_policy.get("source_margin_mode") or ""),
            leverage=float(effective_lev),
            now=now,
        )
        leverage_setup["policy"] = leverage_policy
        if not leverage_setup.get("ok"):
            error_text = str(leverage_setup.get("error") or "leverage_setup_failed")
            backoff_payload = register_order_error_backoff(state, error_text, now, args)
            failure_payload = register_entry_failure(state, attempt_key, error_text, now, args)
            record_session_order(
                args,
                now=now,
                symbol=str(trade.get("symbol") or args.live_symbol),
                side=str(trade["side"]),
                type_="OPEN",
                price=expected_price,
                qty=0.0,
                status="REJECTED",
                reason="leverage_setup_failed",
                extra={"leverage_setup": leverage_setup, "order_error_backoff": backoff_payload, "entry_failure": failure_payload, "attempt_key": attempt_key},
            )
            return {
                "type": "live_entry_blocked",
                "key": trade.get("key"),
                "fill_type": fill_type,
                "reason": "leverage_setup_failed",
                "error": error_text,
                "leverage_setup": leverage_setup,
                "exchange_preflight": preflight,
                "requested_notional": notional,
                "effective_order_notional": order_notional,
                "attempt_key": attempt_key,
                "backoff": backoff_payload,
                "entry_failure": failure_payload,
            }
    client_order_id = stable_client_order_id(
        "entry",
        args.run_id,
        trade.get("key"),
        trade.get("lead_position_id"),
        trade.get("opened_at_utc"),
        fill_type,
        trade.get("next_level_idx"),
    )
    attempt_guard = order_attempt_guard_active(state, now, args)
    if attempt_guard:
        return {
            "type": "live_entry_blocked",
            "key": trade.get("key"),
            "fill_type": fill_type,
            "reason": str(attempt_guard.get("reason") or "order_attempt_guard"),
            "order_attempt_guard": attempt_guard,
            "requested_notional": notional,
            "effective_order_notional": order_notional,
            "attempt_key": attempt_key,
        }
    post_attempt = register_order_post_attempt(state, now, args, action="OPEN", symbol=trade["symbol"], side=str(trade["side"]))
    submitted = submit_open(args, trade["symbol"], str(trade["side"]), expected_price, order_notional, client_order_id)
    if not submitted.get("ok"):
        error_text = str(submitted.get("error") or "unknown_order_error")
        backoff_payload = register_order_error_backoff(state, error_text, now, args)
        failure_payload = register_entry_failure(state, attempt_key, error_text, now, args)
        record_session_order(
            args,
            now=now,
            symbol=str(trade.get("symbol") or args.live_symbol),
            side=str(trade["side"]),
            type_="OPEN",
            price=expected_price,
            qty=0.0,
            status="REJECTED",
            reason=error_text,
            exchange_order_id=str(submitted.get("exchange_order_id") or ""),
            extra={**submitted, "order_error_backoff": backoff_payload, "entry_failure": failure_payload, "attempt_key": attempt_key, "post_attempt": post_attempt},
        )
        return {
            "type": "live_entry_failed",
            "key": trade.get("key"),
            "fill_type": fill_type,
            "reason": reason,
            "error": error_text,
            "guard": guard_detail,
            "exchange_preflight": preflight,
            "leverage_setup": leverage_setup,
            "requested_notional": notional,
            "effective_order_notional": order_notional,
            "attempt_key": attempt_key,
            "backoff": backoff_payload,
            "entry_failure": failure_payload,
            "post_attempt": post_attempt,
        }
    order = safe_order(submitted.get("order"))
    fill_price = float(submitted.get("fill_price") or avg_price(order, expected_price))
    qty = float(submitted.get("order_qty") or submitted.get("qty") or 0.0)
    position_qty = float(submitted.get("qty") or qty)
    position_entry = float(submitted.get("entry") or fill_price)
    live_notional = qty * fill_price
    if submitted.get("exchange_position"):
        trade["qty"] = position_qty
        trade["notional"] = position_qty * position_entry
        trade["avg_entry"] = position_entry
    else:
        trade["qty"] = float(trade.get("qty") or 0.0) + qty
        trade["notional"] = float(trade.get("notional") or 0.0) + live_notional
        trade["avg_entry"] = trade["notional"] / max(trade["qty"], 1e-12)
    fee = order_fee_usdt(submitted.get("order") if isinstance(submitted.get("order"), dict) else order)
    trade["fees_paid"] = float(trade.get("fees_paid") or 0.0) + fee
    entry_slip = signed_slip_bp(str(trade["side"]), expected_price, fill_price, is_close=False)
    entry_lag = fill_lag_sec(now, submitted.get("fill_dt"))
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
        "effective_order_notional": order_notional,
        "live_notional": live_notional,
        "source_leverage": leverage_policy.get("source_leverage"),
        "source_leverage_raw": leverage_policy.get("source_leverage_raw"),
        "effective_leverage": leverage_policy.get("effective_leverage"),
        "source_leverage_mode": leverage_policy.get("mode"),
        "source_margin_mode": leverage_policy.get("source_margin_mode"),
        "lead_margin_balance_usdt": trade.get("lead_margin_balance_usdt"),
        "source_position_margin_usdt": trade.get("source_position_margin_usdt"),
        "source_position_margin_source": trade.get("source_position_margin_source"),
        "source_margin_fraction": trade.get("source_margin_fraction"),
        "source_margin_fraction_reason": trade.get("source_margin_fraction_reason"),
        "leverage_setup": leverage_setup,
        "post_attempt": post_attempt,
        "qty": qty,
        "requested_base_qty": submitted.get("requested_base_qty"),
        "exchange_preflight": preflight,
        "normalized_contract_amount": submitted.get("normalized_contract_amount"),
        "filled_contracts": submitted.get("filled_contracts"),
        "filled_base_qty": submitted.get("filled_base_qty"),
        "post_trade_position_qty": submitted.get("post_trade_position_qty"),
        "position_qty": position_qty,
        "position_entry": position_entry,
        "fee_usdt": fee,
        "entry_slip_bp": entry_slip,
        "entry_lag_sec": entry_lag,
        "mark": mark,
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
    record_order_execution_comparison(
        args,
        now=now,
        action="OPEN",
        trade=trade,
        signal_price=expected_price,
        exchange_fill_price=fill_price,
        exchange_fill_time_utc=submitted.get("fill_dt"),
        source_history=None,
        source_meta={"valid": False, "reason": "open_no_source_history"},
        exchange_order_id=str(submitted.get("exchange_order_id") or ""),
        client_order_id=client_order_id,
        extra={"fill_type": fill_type, "reason": reason},
    )
    upsert_session_position(args, trade, status="OPEN", now=now, exchange_order_id=str(submitted.get("exchange_order_id") or ""))
    record_session_order(
        args,
        now=now,
        symbol=str(trade.get("symbol") or args.live_symbol),
        side=str(trade["side"]),
        type_="OPEN",
        price=fill_price,
        qty=qty,
        status="FILLED",
        reason=reason,
        exchange_order_id=str(submitted.get("exchange_order_id") or ""),
        extra={"fill": fill, "submitted": submitted, "exchange_preflight": preflight, "leverage_setup": leverage_setup},
    )
    clear_order_error_backoff(state)
    clear_entry_failure(state, attempt_key)
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
    backoff = order_error_backoff_active(state, now)
    if backoff:
        return None, {
            "type": "live_exit_blocked",
            "key": trade.get("key"),
            "reason": str(backoff.get("reason") or "order_error_backoff"),
            "backoff": backoff,
        }
    client_order_id = stable_client_order_id("exit", args.run_id, trade.get("key"), trade.get("lead_position_id"), trade.get("opened_at_utc"))
    attempt_guard = order_attempt_guard_active(state, now, args)
    if attempt_guard:
        return None, {
            "type": "live_exit_blocked",
            "key": trade.get("key"),
            "reason": str(attempt_guard.get("reason") or "order_attempt_guard"),
            "order_attempt_guard": attempt_guard,
        }
    post_attempt = register_order_post_attempt(state, now, args, action="CLOSE", symbol=trade["symbol"], side=str(trade["side"]))
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
            symbol=str(trade.get("symbol") or args.live_symbol),
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
        error_text = str(submitted.get("error") or "unknown_order_error")
        backoff_payload = register_order_error_backoff(state, error_text, now, args)
        record_session_order(
            args,
            now=now,
            symbol=str(trade.get("symbol") or args.live_symbol),
            side=str(trade["side"]),
            type_="CLOSE",
            price=expected_exit,
            qty=float(trade.get("qty") or 0.0),
            status="REJECTED",
            reason=error_text,
            exchange_order_id=str(submitted.get("exchange_order_id") or ""),
            extra={**submitted, "order_error_backoff": backoff_payload, "post_attempt": post_attempt},
        )
        return None, {
            "type": "live_exit_failed",
            "key": trade.get("key"),
            "reason": reason,
            "error": error_text,
            "backoff": backoff_payload,
            "post_attempt": post_attempt,
        }
    order = safe_order(submitted.get("order"))
    exit_price = float(submitted.get("fill_price") or avg_price(order, expected_exit))
    exit_lag = fill_lag_sec(now, submitted.get("fill_dt"))
    exit_fee = order_fee_usdt(submitted.get("order") if isinstance(submitted.get("order"), dict) else order)
    source_meta = {"valid": False, "reason": "missing_history"}
    valid_history = None
    if history_row:
        valid_history, source_meta = validate_source_history_match(trade, history_row, now)
    slip_reference = float(valid_history.get("avg_close_price")) if valid_history and valid_history.get("avg_close_price") else expected_exit
    exit_slip = signed_slip_bp(str(trade["side"]), slip_reference, exit_price, is_close=True)
    closed = paper.close_trade(trade, now=now, expected_exit=exit_price, mark=mark, reason=reason, history_row=valid_history)
    gross = paper.ret_for(str(trade["side"]), float(trade["avg_entry"]), exit_price) * float(trade["notional"])
    closed["paper_pnl_usdt"] = gross - float(trade.get("fees_paid") or 0.0) - exit_fee
    closed["paper_exit_price"] = exit_price
    closed["exit_fee"] = exit_fee
    closed["paper_only"] = False
    closed["live_exit_order"] = order
    closed["live_exit_price"] = exit_price
    closed["exit_slip_bp"] = exit_slip
    closed["exit_expected_price"] = expected_exit
    closed["exit_slip_reference_price"] = slip_reference
    closed["history_exit_match"] = source_meta
    closed["exit_lag_sec"] = exit_lag
    closed["exit_fill_ts"] = submitted.get("fill_dt")
    closed["exit_mark_price"] = mark
    closed["client_order_id"] = client_order_id
    closed["exchange_order_id"] = submitted.get("exchange_order_id")
    closed["exit_post_attempt"] = post_attempt
    upsert_session_position(args, closed, status="CLOSED", now=now, exchange_order_id=str(submitted.get("exchange_order_id") or ""), exit_fill=exit_price, close_reason=reason)
    record_session_order(
        args,
        now=now,
        symbol=str(trade.get("symbol") or args.live_symbol),
        side=str(trade["side"]),
        type_="CLOSE",
        price=exit_price,
        qty=float(submitted.get("qty") or 0.0),
        status="FILLED",
        reason=reason,
        exchange_order_id=str(submitted.get("exchange_order_id") or ""),
        extra={"closed": closed, "submitted": submitted, "source_history_match": source_meta, "post_attempt": post_attempt},
    )
    record_order_execution_comparison(
        args,
        now=now,
        action="CLOSE",
        trade=trade,
        signal_price=slip_reference,
        exchange_fill_price=exit_price,
        exchange_fill_time_utc=submitted.get("fill_dt"),
        source_history=valid_history,
        source_meta=source_meta,
        exchange_order_id=str(submitted.get("exchange_order_id") or ""),
        client_order_id=client_order_id,
        extra={"expected_exit_before_validation": expected_exit, "reason": reason},
    )
    clear_order_error_backoff(state)
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
    seed_events = seed_open_trades_from_exchange(state, positions, mark, now, args)
    plan = copy_signal_meta.build_strategy_intents(state, positions, history, mark, now, args, allow_dca=allow_dca, iso_fn=paper.iso)
    events: List[Dict[str, Any]] = list(seed_events)
    events.extend(plan.get("events") or [])
    current_keys = set(plan.get("current_keys") or set())
    current_source, _source_size_filter_events = copy_signal_meta.filter_source_positions(positions, symbol=args.symbol, long_only=bool(args.long_only))
    open_trades: Dict[str, Dict[str, Any]] = state.setdefault("open_trades", {})
    dca_blocked_keys = set()

    for key in sorted(current_keys & set(open_trades)):
        trade = open_trades[key]
        sync_meta = sync_trade_from_exchange(args, state, trade)
        if sync_meta.get("synced"):
            events.append({"type": "exchange_position_synced", "key": key, **sync_meta})

    events.extend(apply_source_size_sync(state, open_trades, current_source, mark, now, args))

    for intent in plan.get("intents") or []:
        key = str(intent.get("key") or "")
        if intent.get("action") == "OPEN":
            if intent.get("intent_type") == "dca_entry" and key in dca_blocked_keys:
                continue
            trade = intent["trade"]
            if intent.get("intent_type") == "dca_entry" and key not in open_trades:
                events.append({"type": "live_entry_blocked", "key": key, "fill_type": intent.get("fill_type"), "reason": "strategy_intent_without_open_trade"})
                dca_blocked_keys.add(key)
                continue
            source_pos = current_source.get(key)
            if source_pos:
                resize_event = resize_trade_source_box(trade, source_size_snapshot(source_pos), now, args, reason=str(intent.get("intent_type") or "strategy_intent"))
                if resize_event:
                    events.append(resize_event)
            intent_notional = float(intent["notional"])
            if intent.get("intent_type") == "open_entry":
                intent_notional = float(trade.get("base_notional") or intent_notional)
            elif intent.get("intent_type") == "dca_entry":
                level_idx = int(intent.get("level_idx") or 0)
                add_notionals = list(trade.get("add_notionals") or [])
                if 0 <= level_idx < len(add_notionals):
                    intent_notional = float(add_notionals[level_idx])
            event = live_add_fill(
                state,
                trade,
                now=now,
                expected_price=float(intent["expected_price"]),
                notional=intent_notional,
                fill_type=str(intent["fill_type"]),
                reason=str(intent["reason"]),
                mark=mark,
                args=args,
            )
            if event["type"] == "live_fill" and intent.get("intent_type") == "open_entry":
                open_trades[key] = trade
            if event["type"] == "live_fill" and intent.get("intent_type") == "dca_entry":
                trade["next_level_idx"] = int(intent.get("level_idx") or 0) + 1
            elif event["type"] != "live_fill":
                dca_blocked_keys.add(key)
            events.append(event)
            continue
        if intent.get("action") != "CLOSE":
            events.append({"type": "strategy_intent_ignored", "key": key, "reason": "unsupported_intent", "intent": intent})
            continue
        trade = intent["trade"]
        closed, event = live_close_trade(
            state,
            trade,
            now=now,
            expected_exit=float(intent["expected_exit"]),
            mark=mark,
            reason=str(intent["reason"]),
            history_row=intent.get("history_row"),
            args=args,
        )
        if event.get("type") == "live_exit":
            event["strategy_policy"] = intent.get("strategy_policy")
            events.append(event)
        else:
            events.append(event)
        if closed is None:
            continue
        state["equity"] = float(state.get("equity") or args.initial_equity) + float(closed["paper_pnl_usdt"])
        state.setdefault("closed_trades", []).append(closed)
        if key in open_trades:
            del open_trades[key]
    return events


def status_payload(state: Dict[str, Any], mark: Optional[float], now: datetime, events: List[Dict[str, Any]], input_meta: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    payload = paper.status_payload(state, mark, now, events, input_meta, args)
    payload["paper_only"] = False
    payload["live_order_code_present"] = True
    payload["live_exchange"] = args.live_exchange
    payload["live_symbol"] = args.live_symbol
    payload["live_multi_symbol_mode"] = live_multi_symbol_mode(args)
    payload["symbol_filter"] = args.symbol
    payload["symbol_marks"] = getattr(args, "_symbol_marks", {})
    payload["callme_meta_strategy"] = (getattr(args, "_live_config", {}) or {}).get("_meta_strategy")
    payload["live_exchange_profile"] = args.live_exchange_profile
    payload["position_mode"] = args.position_mode
    payload["source_leverage_policy"] = {
        "source_leverage_mode": getattr(args, "source_leverage_mode", "ignore"),
        "source_margin_mode_override": getattr(args, "source_margin_mode_override", ""),
        "fixed_source_leverage": getattr(args, "fixed_source_leverage", 0.0),
        "max_source_leverage": getattr(args, "max_source_leverage", 0.0),
    }
    payload["source_size_sync"] = {
        "mode": getattr(args, "source_size_sync_mode", "off"),
        "interval_sec": getattr(args, "source_size_sync_interval_sec", DEFAULT_SOURCE_SIZE_SYNC_INTERVAL_SEC),
        "min_change_pct": getattr(args, "source_size_sync_min_change_pct", 0.0),
        "min_adjust_notional_usdt": getattr(args, "source_size_sync_min_adjust_notional_usdt", 0.0),
        "recent_observations": (state.get("source_size_observations") or [])[-20:] if isinstance(state.get("source_size_observations"), list) else [],
    }
    open_trades = state.get("open_trades") if isinstance(state.get("open_trades"), dict) else {}
    payload["source_box"] = {
        key: {
            "symbol": trade.get("symbol"),
            "side": trade.get("side"),
            "source_margin_fraction": trade.get("source_margin_fraction"),
            "source_position_margin_usdt": trade.get("source_position_margin_usdt"),
            "lead_margin_balance_usdt": trade.get("lead_margin_balance_usdt"),
            "target_notional": trade.get("target_notional"),
            "base_notional": trade.get("base_notional"),
            "add_notionals": trade.get("add_notionals"),
            "source_box_margin_usdt": ((trade.get("source_box_target_meta") or {}).get("source_box_margin_usdt") if isinstance(trade.get("source_box_target_meta"), dict) else None),
            "source_box_ratio": trade.get("source_box_ratio"),
            "source_box_target_basis": trade.get("source_box_target_basis"),
            "source_box_current_target_notional": trade.get("source_box_current_target_notional"),
            "source_box_last_resize_utc": trade.get("source_box_last_resize_utc"),
        }
        for key, trade in sorted(open_trades.items())
    }
    payload["auth_probe"] = getattr(args, "_auth_probe", {})
    payload["exchange_switch_reset"] = getattr(args, "_exchange_switch_reset", {})
    payload["copy_poll_interval_sec"] = args.interval_sec
    payload["dca_eval_interval_sec"] = args.dca_eval_interval_sec
    payload["history_poll_interval_sec"] = args.history_poll_interval_sec
    payload["mark_poll_interval_sec"] = getattr(args, "mark_poll_interval_sec", DEFAULT_MARK_POLL_INTERVAL_SEC)
    payload["dca_eval_meta"] = getattr(args, "_last_dca_eval_meta", {})
    payload["control"] = control_state(args)
    payload["session_db"] = args.session_db
    payload["run_id"] = args.run_id
    payload["active_pointers"] = active_pointer_sanity(args)
    payload["order_sync_wait_sec"] = args.order_sync_wait_sec
    payload["order_error_backoff"] = state.get("order_error_backoff") if isinstance(state.get("order_error_backoff"), dict) else {}
    payload["order_attempts"] = state.get("order_attempts") if isinstance(state.get("order_attempts"), dict) else {}
    payload["max_order_attempts_per_hour"] = getattr(args, "max_order_attempts_per_hour", DEFAULT_MAX_ORDER_ATTEMPTS_PER_HOUR)
    payload["order_post_throttle_sec"] = getattr(args, "order_post_throttle_sec", DEFAULT_ORDER_POST_THROTTLE_SEC)
    payload["entry_failures"] = state.get("entry_failures") if isinstance(state.get("entry_failures"), dict) else {}
    payload["leverage_set_cache"] = state.get("leverage_set_cache") if isinstance(state.get("leverage_set_cache"), dict) else {}
    payload["last_leverage_setup"] = state.get("last_leverage_setup") if isinstance(state.get("last_leverage_setup"), dict) else {}
    payload["runtime_sizing"] = runtime_sizing_payload(state, args)
    payload["protection"] = getattr(args, "_last_protection", {})
    return payload


def poll_once(args: argparse.Namespace) -> Dict[str, Any]:
    now = paper.utc_now()
    write_active_pointers(args)
    controls = control_state(args)
    handle_hot_stop_if_requested(args, controls, now)
    if controls["kill"]:
        raise SystemExit(f"KILL file present: {controls['kill_path']}")
    state = paper.load_json(Path(args.state_path), paper.default_state(args))
    state["mode"] = "hype_cap100_bingx_live_canary"
    state["paper_only"] = False
    state["live_exchange"] = args.live_exchange
    positions, history, mark, input_meta = load_inputs_live(args, state, now)
    protection = evaluate_live_protection(state, mark, now, input_meta, args)
    args._last_protection = protection
    protection_event = apply_live_protection_side_effects(args, protection, now)
    allow_dca, dca_meta = dca_eval_due(state, now, args)
    args._last_dca_eval_meta = dca_meta
    events = apply_live_snapshot(state, positions, history, mark, now, args, allow_dca=allow_dca)
    if protection_event:
        events.insert(0, protection_event)
    if allow_dca:
        state["last_dca_eval_bucket"] = dca_meta.get("dca_eval_bucket")
        state["last_dca_eval_utc"] = paper.iso(now)
    status = status_payload(state, mark, now, events, input_meta, args)
    status["ui_artifacts"] = emit_ui_artifacts(args, status)
    status["active_pointers"] = active_pointer_sanity(args, status)
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
    ap.description = "Exchange live HYPE cap100 canary."
    ap.set_defaults(out_dir=str(DEFAULT_OUT_DIR), interval_sec=DEFAULT_COPY_POLL_INTERVAL_SEC)
    ap.add_argument("--live-config", default="", help="JSON live launch config with exchange, env aliases, allocation, and safety limits.")
    ap.add_argument("--live-exchange-profile", choices=SUPPORTED_LIVE_EXCHANGE_PROFILES, default="gateio_current")
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--live-exchange", default=None, choices=SUPPORTED_LIVE_EXCHANGES)
    ap.add_argument("--live-symbol", default=None)
    ap.add_argument("--live-api-key-env", default="", help="Env var name containing the exchange API key; value is never printed.")
    ap.add_argument("--live-api-secret-env", default="", help="Env var name containing the exchange API secret; value is never printed.")
    ap.add_argument("--position-mode", choices=["oneway", "hedge"], default=None)
    ap.add_argument("--dca-eval-interval-sec", type=float, default=DEFAULT_DCA_EVAL_INTERVAL_SEC)
    ap.add_argument("--history-poll-interval-sec", type=float, default=DEFAULT_HISTORY_POLL_INTERVAL_SEC)
    ap.add_argument("--mark-poll-interval-sec", type=float, default=DEFAULT_MARK_POLL_INTERVAL_SEC, help="Minimum seconds between public mark fetches when no source/local position is active; 0 fetches every poll.")
    ap.add_argument("--control-dir", default="", help="Directory containing STOP_NEW_ORDERS and KILL files. Defaults to out-dir.")
    ap.add_argument("--session-db", default="")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--order-sync-wait-sec", type=float, default=3.0)
    ap.add_argument("--order-sync-poll-sec", type=float, default=0.25)
    ap.add_argument("--order-error-backoff-sec", type=float, default=DEFAULT_ORDER_ERROR_BACKOFF_SEC, help="Minimum private order POST cooldown after an exchange order error.")
    ap.add_argument("--order-error-circuit-sec", type=float, default=DEFAULT_ORDER_ERROR_CIRCUIT_SEC, help="Long private order POST cooldown after repeated/config/rate-limit errors.")
    ap.add_argument("--order-error-max-consecutive", type=int, default=DEFAULT_ORDER_ERROR_MAX_CONSECUTIVE, help="Consecutive order errors before using circuit cooldown.")
    ap.add_argument("--max-order-attempts-per-hour", type=int, default=DEFAULT_MAX_ORDER_ATTEMPTS_PER_HOUR, help="Maximum live order POST attempts per UTC hour, counting rejected attempts; 0 disables.")
    ap.add_argument("--order-post-throttle-sec", type=float, default=DEFAULT_ORDER_POST_THROTTLE_SEC, help="Minimum state-backed spacing between live trade/order POST attempts.")
    ap.add_argument("--entry-failure-cooldown-sec", type=float, default=DEFAULT_ENTRY_FAILURE_COOLDOWN_SEC, help="Cooldown for the same symbol/lead/fill-level after rejected entry.")
    ap.add_argument("--source-leverage-mode", choices=SUPPORTED_SOURCE_LEVERAGE_MODES, default="ignore", help="How to apply Binance copy-source leverage before follower opens/DCA legs.")
    ap.add_argument("--fixed-source-leverage", type=float, default=0.0, help="Fixed leverage used when --source-leverage-mode=fixed.")
    ap.add_argument("--max-source-leverage", type=float, default=0.0, help="Optional cap for effective source leverage; 0 means uncapped.")
    ap.add_argument("--source-margin-mode-override", choices=("", "cross", "isolated"), default="", help="Optional forced margin mode for follower leverage setup.")
    ap.add_argument("--source-size-sync-mode", choices=("off", "ratio"), default="off", help="Follow source position amount changes with proportional follower add/reduce orders.")
    ap.add_argument("--source-size-sync-interval-sec", type=float, default=DEFAULT_SOURCE_SIZE_SYNC_INTERVAL_SEC, help="Minimum seconds between source size sync checks per open trade.")
    ap.add_argument("--source-size-sync-min-change-pct", type=float, default=0.0, help="Ignore source size ratio changes below this percent.")
    ap.add_argument("--source-size-sync-min-adjust-notional-usdt", type=float, default=0.0, help="Ignore proportional follower add/reduce deltas below this notional.")
    ap.add_argument("--hot-restart-snapshot-path", default="", help="Where HOT_STOP writes a restart snapshot. Defaults to out-dir/HOT_RESTART_SNAPSHOT.json.")
    ap.add_argument("--resume-snapshot", default="", help="Load state from a HOT_STOP snapshot before starting.")
    ap.add_argument("--resume-snapshot-overwrite", action="store_true", help="Allow --resume-snapshot to replace an existing state-path.")
    ap.add_argument("--live-cache-npz-path", default="", help="Live OHLCV NPZ artifact path. Defaults to out-dir/live_mark_ohlcv.npz.")
    ap.add_argument("--live-cache-npz-max-bars", type=int, default=10000, help="Max rows retained in the live OHLCV NPZ artifact.")
    ap.add_argument("--stdout-log-path", default="", help="Current stdout log path to publish via ACTIVE_LOG_PATH.txt.")
    ap.add_argument("--protection-account-loss-stop-usdt", type=float, default=0.0, help="Block new entries when daily realized+unrealized PnL reaches this negative USD threshold.")
    ap.add_argument("--protection-floating-pnl-stop-usdt", type=float, default=0.0, help="Block new entries when open floating PnL reaches this negative USD threshold.")
    ap.add_argument("--protection-emergency-account-loss-usdt", type=float, default=0.0, help="Mark emergency protection when daily realized+unrealized PnL reaches this negative USD threshold.")
    ap.add_argument("--protection-stale-market-sec", type=float, default=0.0, help="Block new entries after stale cached mark/position inputs following fetch errors.")
    ap.add_argument("--protection-require-book-ok", action="store_true", help="Block new entries when mark metadata reports book_ok=false.")
    ap.add_argument("--protection-require-premium-ok", action="store_true", help="Block new entries when mark metadata reports premium_ok=false.")
    ap.add_argument("--protection-auto-stop-new-orders", action="store_true", help="Create STOP_NEW_ORDERS when protection blocks new entries.")
    return ap


def apply_live_exchange_profile(args: argparse.Namespace) -> Dict[str, Any]:
    profile_name = str(getattr(args, "live_exchange_profile", "") or "gateio_current")
    profile = LIVE_EXCHANGE_PROFILES.get(profile_name)
    if profile is None:
        raise SystemExit(f"unsupported --live-exchange-profile: {profile_name}")
    applied: Dict[str, Any] = {"profile": profile_name, "defaults_applied": []}
    for attr in ("live_exchange", "live_symbol", "position_mode", "env_file"):
        if getattr(args, attr, None):
            continue
        setattr(args, attr, profile[attr])
        applied["defaults_applied"].append(attr)
    return applied


def normalize_paths(args: argparse.Namespace) -> None:
    args._live_config_resolution = apply_live_config(args)
    args._live_exchange_profile_resolution = apply_live_exchange_profile(args)
    if str(getattr(args, "live_exchange", "") or "").lower() == "bingx" and float(getattr(args, "interval_sec", 0.0) or 0.0) < 60.0:
        args.interval_sec = 60.0
        args._bingx_interval_clamped = True
    paper.normalize_paths(args)
    out_dir = Path(args.out_dir)
    if args.session_db and not Path(args.session_db).is_absolute() and Path(args.session_db).parent == Path("."):
        args.session_db = str(out_dir / args.session_db)
    write_active_pointers(args)


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
    if args.order_error_backoff_sec <= 0 or args.order_error_circuit_sec <= 0 or args.entry_failure_cooldown_sec <= 0:
        raise SystemExit("order error cooldowns must be positive")
    if args.order_error_max_consecutive <= 0:
        raise SystemExit("--order-error-max-consecutive must be positive")
    if args.max_order_attempts_per_hour < 0:
        raise SystemExit("--max-order-attempts-per-hour must be non-negative")
    if args.order_post_throttle_sec < 0:
        raise SystemExit("--order-post-throttle-sec must be non-negative")
    if args.mark_poll_interval_sec < 0:
        raise SystemExit("--mark-poll-interval-sec must be non-negative")
    if str(args.live_exchange or "").lower() == "bingx" and float(args.interval_sec or 0.0) < 60.0:
        raise SystemExit("BingX live poll interval must be at least 60 seconds")
    if args.max_source_leverage < 0:
        raise SystemExit("--max-source-leverage must be non-negative")
    if args.fixed_source_leverage < 0:
        raise SystemExit("--fixed-source-leverage must be non-negative")
    if args.source_leverage_mode == "fixed" and args.fixed_source_leverage <= 0:
        raise SystemExit("--fixed-source-leverage is required when --source-leverage-mode=fixed")
    if args.source_margin_mode_override and normalize_source_margin_mode(args.source_margin_mode_override) not in {"cross", "isolated"}:
        raise SystemExit("--source-margin-mode-override must be cross or isolated")
    if args.live_cache_npz_max_bars <= 0:
        raise SystemExit("--live-cache-npz-max-bars must be positive")
    for name in (
        "protection_account_loss_stop_usdt",
        "protection_floating_pnl_stop_usdt",
        "protection_emergency_account_loss_usdt",
        "protection_stale_market_sec",
    ):
        if float(getattr(args, name, 0.0) or 0.0) < 0:
            raise SystemExit(f"--{name.replace('_', '-')} must be non-negative")
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
    write_active_pointers(args)
    load_resume_snapshot(args)
    args._exchange_switch_reset = reset_exchange_failures_on_switch(args)
    args._auth_probe = auth_probe(args)
    while True:
        print(json.dumps(poll_once(args), ensure_ascii=False, indent=2, sort_keys=True), flush=True)
        if not args.loop:
            break
        if paper.deadline_reached(paper.utc_now(), args.deadline_utc):
            break
        sleep_until_next_poll(args.interval_sec)


if __name__ == "__main__":
    main()
