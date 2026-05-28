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
DEFAULT_LIVE_SYMBOL = "HYPE-USDT"
SUPPORTED_LIVE_EXCHANGES = ("bingx", "gateio")
SUPPORTED_LIVE_EXCHANGE_PROFILES = ("gateio_current", "bingx_legacy")
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
}
DEFAULT_COPY_POLL_INTERVAL_SEC = 1.0
DEFAULT_DCA_EVAL_INTERVAL_SEC = 60.0
DEFAULT_HISTORY_POLL_INTERVAL_SEC = 60.0
DEFAULT_ORDER_ERROR_BACKOFF_SEC = 300.0
DEFAULT_ORDER_ERROR_CIRCUIT_SEC = 1800.0
DEFAULT_ORDER_ERROR_MAX_CONSECUTIVE = 3
DEFAULT_ENTRY_FAILURE_COOLDOWN_SEC = 3600.0


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
            symbol=args.live_symbol,
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


def export_match_ready_trade_history(args: argparse.Namespace) -> Optional[str]:
    rows = _read_session_orders_for_artifacts(str(args.session_db or ""), str(args.run_id or ""))
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


def export_live_chart_events(args: argparse.Namespace) -> Dict[str, Optional[str]]:
    rows = _read_session_orders_for_artifacts(str(args.session_db or ""), str(args.run_id or ""))
    if not rows:
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
            }
        )
    jsonl_path = out_dir / "live_chart_events.jsonl"
    csv_path = out_dir / "live_chart_events.csv"
    with jsonl_path.open("w", encoding="utf-8") as fh:
        for event in events[-10000:]:
            fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["ts", "type", "side", "symbol", "price", "qty", "order_id", "position_id", "pnl", "status", "text"],
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
        "long_only": bool(args.long_only),
        "risk": {
            "initial_equity": args.initial_equity,
            "initial_target_notional": args.initial_target_notional,
            "max_gross_notional_usdt": args.max_gross_notional_usdt,
            "max_one_side_notional_usdt": args.max_one_side_notional_usdt,
            "max_daily_loss_usdt": args.max_daily_loss_usdt,
            "max_orders_per_hour": args.max_orders_per_hour,
            "deadline_utc": args.deadline_utc,
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
    delay = float(getattr(args, "order_error_backoff_sec", DEFAULT_ORDER_ERROR_BACKOFF_SEC) or DEFAULT_ORDER_ERROR_BACKOFF_SEC)
    reason = "order_error_backoff"
    if consecutive >= max_consecutive:
        delay = max(delay, float(getattr(args, "order_error_circuit_sec", DEFAULT_ORDER_ERROR_CIRCUIT_SEC) or DEFAULT_ORDER_ERROR_CIRCUIT_SEC))
        reason = "order_error_circuit_breaker"
    until_ts = now.timestamp() + max(delay, 1.0)
    payload = {
        "reason": reason,
        "last_error": text,
        "consecutive": consecutive,
        "until_ts": until_ts,
        "until_utc": paper.iso(datetime.fromtimestamp(until_ts, tz=timezone.utc)),
    }
    state["order_error_backoff"] = payload
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
    balance_params = live_balance_params(args.live_exchange)
    bal = client.ex.fetch_balance(balance_params) if balance_params else client.ex.fetch_balance()
    out["fetch_balance_ok"] = True
    out["balance_params"] = balance_params
    out["balance_keys"] = sorted(list((bal or {}).keys()))[:20] if isinstance(bal, dict) else []
    return out


def live_balance_params(exchange: str) -> Dict[str, Any]:
    if str(exchange or "").lower() == "gateio":
        return {"type": "swap", "settle": "USDT"}
    return {}


def live_order_params(client_order_id: str, side: str, reduce_only: bool, position_mode: str = "oneway", exchange: str = "bingx") -> Dict[str, Any]:
    ex = str(exchange or "").lower()
    if ex == "gateio":
        params: Dict[str, Any] = {"text": gateio_client_order_text(client_order_id)}
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
    contract_size = live_market_contract_size(client, ccxt_symbol) if str(args.live_exchange).lower() == "gateio" else 1.0
    raw_amount = float(base_qty or 0.0) / max(contract_size, 1e-12)
    max_amount = None if max_base_qty is None else float(max_base_qty or 0.0) / max(contract_size, 1e-12)
    order_amount = _normalize_order_qty(client, ccxt_symbol, raw_amount, is_close=is_close, max_qty=max_amount)
    return float(order_amount or 0.0), float(order_amount or 0.0) * contract_size, contract_size


def live_order_filled_base_qty(args: argparse.Namespace, client: CCXTFetcher, ccxt_symbol: str, order: Dict[str, Any], fallback_order_amount: float) -> float:
    order_amount = order_filled_qty(order, fallback_order_amount)
    if str(args.live_exchange).lower() == "gateio":
        return float(order_amount or 0.0) * live_market_contract_size(client, ccxt_symbol)
    return float(order_amount or 0.0)


def live_fetch_exchange_position(args: argparse.Namespace, client: CCXTFetcher, ccxt_symbol: str, side: str) -> Optional[Dict[str, Any]]:
    pos = _fetch_exchange_position(client, ccxt_symbol, side)
    if not isinstance(pos, dict):
        return pos
    if str(args.live_exchange).lower() == "gateio" and not pos.get("_qty_unit") == "base":
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
    ccxt_symbol = client.resolve_symbol(symbol) or client.resolve_symbol(args.live_symbol) or args.live_symbol
    base_qty = float(notional) / max(float(expected_price), 1e-12)
    min_base_qty = float(paper.CHAMPION_PARAMS.get("min_order_qty_hype") or 0.0) if str(args.symbol).upper() == "HYPEUSDT" else 0.0
    if min_base_qty > 0:
        base_qty = max(base_qty, min_base_qty)
    order_amount, expected_base_qty, contract_size = live_order_amount_from_base_qty(args, client, ccxt_symbol, base_qty, is_close=False)
    if order_amount <= 0:
        return {"ok": False, "error": "open_qty_zero_after_normalize", "qty": expected_base_qty, "order_amount": order_amount}
    params = live_order_params(client_order_id, side, reduce_only=False, position_mode=args.position_mode, exchange=args.live_exchange)
    order_side = "buy" if side.upper() == "LONG" else "sell"
    try:
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
    if fill_px is None:
        return {"ok": False, "error": "open_timeout_no_fill", "qty": expected_base_qty, "order_amount": order_amount, "ccxt_symbol": ccxt_symbol, "order": fetched_order or od, "exchange_order_id": ex_order_id}
    ex_pos = live_fetch_exchange_position(args, client, ccxt_symbol, side)
    order = fetched_order or od
    order_qty = live_order_filled_base_qty(args, client, ccxt_symbol, order if isinstance(order, dict) else {}, order_amount)
    return {
        "ok": True,
        "order": order,
        "order_amount": order_amount,
        "contract_size": contract_size,
        "requested_base_qty": base_qty,
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
        "exchange_order_id": ex_order_id,
        "exchange_position": ex_pos,
    }


def submit_close(args: argparse.Namespace, symbol: str, side: str, qty: float, client_order_id: str) -> Dict[str, Any]:
    client = live_client(args)
    ccxt_symbol = client.resolve_symbol(symbol) or client.resolve_symbol(args.live_symbol) or args.live_symbol
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
        upsert_session_position(args, trade, status="OPEN", now=paper.utc_now(), exchange_order_id=str(trade.get("exchange_order_id") or ""))
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
        args.run_id,
        trade.get("key"),
        trade.get("lead_position_id"),
        trade.get("opened_at_utc"),
        fill_type,
        trade.get("next_level_idx"),
    )
    submitted = submit_open(args, trade["symbol"], str(trade["side"]), expected_price, notional, client_order_id)
    if not submitted.get("ok"):
        error_text = str(submitted.get("error") or "unknown_order_error")
        backoff_payload = register_order_error_backoff(state, error_text, now, args)
        failure_payload = register_entry_failure(state, attempt_key, error_text, now, args)
        record_session_order(
            args,
            now=now,
            symbol=args.live_symbol,
            side=str(trade["side"]),
            type_="OPEN",
            price=expected_price,
            qty=0.0,
            status="REJECTED",
            reason=error_text,
            exchange_order_id=str(submitted.get("exchange_order_id") or ""),
            extra={**submitted, "order_error_backoff": backoff_payload, "entry_failure": failure_payload, "attempt_key": attempt_key},
        )
        return {
            "type": "live_entry_failed",
            "key": trade.get("key"),
            "fill_type": fill_type,
            "reason": reason,
            "error": error_text,
            "guard": guard_detail,
            "requested_notional": notional,
            "attempt_key": attempt_key,
            "backoff": backoff_payload,
            "entry_failure": failure_payload,
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
        "live_notional": live_notional,
        "qty": qty,
        "requested_base_qty": submitted.get("requested_base_qty"),
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
        error_text = str(submitted.get("error") or "unknown_order_error")
        backoff_payload = register_order_error_backoff(state, error_text, now, args)
        record_session_order(
            args,
            now=now,
            symbol=args.live_symbol,
            side=str(trade["side"]),
            type_="CLOSE",
            price=expected_exit,
            qty=float(trade.get("qty") or 0.0),
            status="REJECTED",
            reason=error_text,
            exchange_order_id=str(submitted.get("exchange_order_id") or ""),
            extra={**submitted, "order_error_backoff": backoff_payload},
        )
        return None, {
            "type": "live_exit_failed",
            "key": trade.get("key"),
            "reason": reason,
            "error": error_text,
            "backoff": backoff_payload,
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
        extra={"closed": closed, "submitted": submitted, "source_history_match": source_meta},
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
    plan = copy_signal_meta.build_strategy_intents(state, positions, history, mark, now, args, allow_dca=allow_dca, iso_fn=paper.iso)
    events: List[Dict[str, Any]] = list(plan.get("events") or [])
    current_keys = set(plan.get("current_keys") or set())
    open_trades: Dict[str, Dict[str, Any]] = state.setdefault("open_trades", {})
    dca_blocked_keys = set()

    for key in sorted(current_keys & set(open_trades)):
        trade = open_trades[key]
        sync_meta = sync_trade_from_exchange(args, trade)
        if sync_meta.get("synced"):
            events.append({"type": "exchange_position_synced", "key": key, **sync_meta})

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
            event = live_add_fill(
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
    payload["live_exchange_profile"] = args.live_exchange_profile
    payload["position_mode"] = args.position_mode
    payload["auth_probe"] = getattr(args, "_auth_probe", {})
    payload["exchange_switch_reset"] = getattr(args, "_exchange_switch_reset", {})
    payload["copy_poll_interval_sec"] = args.interval_sec
    payload["dca_eval_interval_sec"] = args.dca_eval_interval_sec
    payload["history_poll_interval_sec"] = args.history_poll_interval_sec
    payload["dca_eval_meta"] = getattr(args, "_last_dca_eval_meta", {})
    payload["control"] = control_state(args)
    payload["session_db"] = args.session_db
    payload["run_id"] = args.run_id
    payload["active_pointers"] = active_pointer_sanity(args)
    payload["order_sync_wait_sec"] = args.order_sync_wait_sec
    payload["order_error_backoff"] = state.get("order_error_backoff") if isinstance(state.get("order_error_backoff"), dict) else {}
    payload["entry_failures"] = state.get("entry_failures") if isinstance(state.get("entry_failures"), dict) else {}
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
    allow_dca, dca_meta = dca_eval_due(state, now, args)
    args._last_dca_eval_meta = dca_meta
    events = apply_live_snapshot(state, positions, history, mark, now, args, allow_dca=allow_dca)
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
    ap.add_argument("--live-exchange-profile", choices=SUPPORTED_LIVE_EXCHANGE_PROFILES, default="gateio_current")
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--live-exchange", default=None, choices=SUPPORTED_LIVE_EXCHANGES)
    ap.add_argument("--live-symbol", default=None)
    ap.add_argument("--position-mode", choices=["oneway", "hedge"], default=None)
    ap.add_argument("--dca-eval-interval-sec", type=float, default=DEFAULT_DCA_EVAL_INTERVAL_SEC)
    ap.add_argument("--history-poll-interval-sec", type=float, default=DEFAULT_HISTORY_POLL_INTERVAL_SEC)
    ap.add_argument("--control-dir", default="", help="Directory containing STOP_NEW_ORDERS and KILL files. Defaults to out-dir.")
    ap.add_argument("--session-db", default="")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--order-sync-wait-sec", type=float, default=3.0)
    ap.add_argument("--order-sync-poll-sec", type=float, default=0.25)
    ap.add_argument("--order-error-backoff-sec", type=float, default=DEFAULT_ORDER_ERROR_BACKOFF_SEC, help="Minimum private order POST cooldown after an exchange order error.")
    ap.add_argument("--order-error-circuit-sec", type=float, default=DEFAULT_ORDER_ERROR_CIRCUIT_SEC, help="Long private order POST cooldown after repeated/config/rate-limit errors.")
    ap.add_argument("--order-error-max-consecutive", type=int, default=DEFAULT_ORDER_ERROR_MAX_CONSECUTIVE, help="Consecutive order errors before using circuit cooldown.")
    ap.add_argument("--entry-failure-cooldown-sec", type=float, default=DEFAULT_ENTRY_FAILURE_COOLDOWN_SEC, help="Cooldown for the same symbol/lead/fill-level after rejected entry.")
    ap.add_argument("--hot-restart-snapshot-path", default="", help="Where HOT_STOP writes a restart snapshot. Defaults to out-dir/HOT_RESTART_SNAPSHOT.json.")
    ap.add_argument("--resume-snapshot", default="", help="Load state from a HOT_STOP snapshot before starting.")
    ap.add_argument("--resume-snapshot-overwrite", action="store_true", help="Allow --resume-snapshot to replace an existing state-path.")
    ap.add_argument("--live-cache-npz-path", default="", help="Live OHLCV NPZ artifact path. Defaults to out-dir/live_mark_ohlcv.npz.")
    ap.add_argument("--live-cache-npz-max-bars", type=int, default=10000, help="Max rows retained in the live OHLCV NPZ artifact.")
    ap.add_argument("--stdout-log-path", default="", help="Current stdout log path to publish via ACTIVE_LOG_PATH.txt.")
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
    args._live_exchange_profile_resolution = apply_live_exchange_profile(args)
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
    if args.live_cache_npz_max_bars <= 0:
        raise SystemExit("--live-cache-npz-max-bars must be positive")
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
        if paper.utc_now() >= paper.parse_utc(args.deadline_utc):
            break
        sleep_until_next_poll(args.interval_sec)


if __name__ == "__main__":
    main()
