#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Paper-live daemon for darkknighttrade Telegram signals.

This script never places real exchange orders. It listens for fresh Telegram
signals, writes them to JSONL and SQLite, and tracks simulated paper positions.
By default, new signals become pending paper entries and are opened only if
BingX ticker price touches the entry zone before the entry timeout.
"""
import argparse
import asyncio
import datetime as dt
import json
import math
import os
import sqlite3
import traceback
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from telethon import TelegramClient, events

try:
    import ccxt  # type: ignore
except Exception:
    ccxt = None
try:
    import yaml  # type: ignore
except Exception:
    yaml = None

try:
    from .telegram_signal_schema import base_symbol, normalize_telegram_channel, parse_channel_exit_text, parse_signal_text
    from .telegram_v21_one_leg_wrapper import (
        dumps_state as v21_dumps_state,
        load_one_leg_config,
        loads_state as v21_loads_state,
        make_bar as v21_make_bar,
        make_strategy as v21_make_strategy,
        manage_existing_position as v21_manage_existing_position,
        open_external_signal as v21_open_external_signal,
    )
except ImportError:
    from telegram_signal_schema import base_symbol, normalize_telegram_channel, parse_channel_exit_text, parse_signal_text
    from telegram_v21_one_leg_wrapper import (
        dumps_state as v21_dumps_state,
        load_one_leg_config,
        loads_state as v21_loads_state,
        make_bar as v21_make_bar,
        make_strategy as v21_make_strategy,
        manage_existing_position as v21_manage_existing_position,
        open_external_signal as v21_open_external_signal,
    )

DEFAULT_DCA_CONFIG = "obw_platform/configs/V21_strict_trend_stable_live_static9p38.yaml"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def parse_iso_ts(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    if len(text) >= 6 and text[-3] == ":" and text[-6] in ("+", "-"):
        text = text[:-3] + text[-2:]
    try:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S%z"):
            try:
                return dt.datetime.strptime(text, fmt)
            except ValueError:
                continue
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                return dt.datetime.strptime(text, fmt)
            except ValueError:
                continue
    except Exception:
        pass
    return None


def seconds_since(value: Optional[str], now: dt.datetime) -> Optional[float]:
    parsed = parse_iso_ts(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return max((now - parsed).total_seconds(), 0.0)


def fmt_age(value: Optional[str], now: dt.datetime) -> str:
    age = seconds_since(value, now)
    return "none" if age is None else "%.1f" % age


def safe_error_text(exc: BaseException) -> str:
    text = "%s: %s" % (exc.__class__.__name__, exc)
    text = " ".join(text.split())
    return text[:240]


class TelegramPollState:
    def __init__(self) -> None:
        self.started_at = utc_now()
        self.last_message_at = None  # type: Optional[str]
        self.last_signal_at = None  # type: Optional[str]
        self.last_exit_at = None  # type: Optional[str]
        self.last_handler_error_at = None  # type: Optional[str]
        self.last_monitor_at = None  # type: Optional[str]
        self.last_monitor_error_at = None  # type: Optional[str]
        self.last_disconnect_at = None  # type: Optional[str]
        self.last_error = ""  # type: str
        self.messages_seen = 0
        self.signals_seen = 0
        self.channel_exits_seen = 0
        self.duplicates_seen = 0
        self.handler_errors = 0
        self.monitor_errors = 0
        self.disconnects = 0

    def record_message(self) -> None:
        self.messages_seen += 1
        self.last_message_at = utc_now()

    def record_signal(self) -> None:
        self.signals_seen += 1
        self.last_signal_at = utc_now()

    def record_channel_exit(self) -> None:
        self.channel_exits_seen += 1
        self.last_exit_at = utc_now()

    def record_duplicate(self) -> None:
        self.duplicates_seen += 1

    def record_handler_error(self, exc: BaseException) -> None:
        self.handler_errors += 1
        self.last_handler_error_at = utc_now()
        self.last_error = safe_error_text(exc)

    def record_monitor_poll(self) -> None:
        self.last_monitor_at = utc_now()

    def record_monitor_error(self, exc: BaseException) -> None:
        self.monitor_errors += 1
        self.last_monitor_error_at = utc_now()
        self.last_error = safe_error_text(exc)

    def record_disconnect(self, exc: Optional[BaseException] = None) -> None:
        self.disconnects += 1
        self.last_disconnect_at = utc_now()
        if exc is not None:
            self.last_error = safe_error_text(exc)


def format_heartbeat_line(state: TelegramPollState, connected: Optional[bool], now: Optional[dt.datetime] = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    connected_text = "unknown" if connected is None else ("yes" if connected else "no")
    status = "alive"
    if connected is False:
        status = "degraded"
    elif state.last_handler_error_at or state.last_monitor_error_at or state.last_disconnect_at:
        status = "alive_with_recent_errors"
    return (
        "[paper-live HEARTBEAT] status=%s connected=%s uptime_sec=%.1f "
        "telegram_idle_sec=%s monitor_idle_sec=%s messages=%s signals=%s channel_exits=%s duplicates=%s "
        "handler_errors=%s monitor_errors=%s disconnects=%s last_message_at=%s last_signal_at=%s "
        "last_exit_at=%s last_monitor_at=%s last_error=%s"
    ) % (
        status,
        connected_text,
        seconds_since(state.started_at, now) or 0.0,
        fmt_age(state.last_message_at, now),
        fmt_age(state.last_monitor_at, now),
        state.messages_seen,
        state.signals_seen,
        state.channel_exits_seen,
        state.duplicates_seen,
        state.handler_errors,
        state.monitor_errors,
        state.disconnects,
        state.last_message_at or "none",
        state.last_signal_at or "none",
        state.last_exit_at or "none",
        state.last_monitor_at or "none",
        state.last_error or "none",
    )


def client_connected(client: Any) -> Optional[bool]:
    try:
        is_connected = getattr(client, "is_connected", None)
        if callable(is_connected):
            return bool(is_connected())
    except Exception:
        return None
    return None


async def heartbeat_loop(client: Any, state: TelegramPollState, interval_sec: float) -> None:
    if interval_sec <= 0:
        return
    while True:
        await asyncio.sleep(interval_sec)
        print(format_heartbeat_line(state, client_connected(client)), flush=True)


def load_env_file(path: str) -> Path:
    env_path = Path(path)
    if env_path.exists():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return env_path.parent if env_path.exists() else Path.cwd()


def resolve_path(raw: str, base: Path) -> Path:
    p = Path(raw)
    return p if p.is_absolute() else base / p


def table_columns(cur: sqlite3.Cursor, table: str) -> Dict[str, bool]:
    return {str(row[1]): True for row in cur.execute("PRAGMA table_info(%s)" % table)}


def ensure_column(cur: sqlite3.Cursor, table: str, column: str, ddl: str) -> None:
    if column not in table_columns(cur, table):
        cur.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, ddl))


def ensure_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS signals(
        telegram_message_id INTEGER PRIMARY KEY,
        source_channel TEXT,
        ts_utc TEXT,
        symbol TEXT,
        side TEXT,
        leverage INTEGER,
        entry_low REAL,
        entry_high REAL,
        tp1 REAL,
        tp2 REAL,
        tp3 REAL,
        sl REAL,
        raw_text TEXT,
        received_at TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS orders(
        order_id TEXT PRIMARY KEY,
        signal_id INTEGER,
        ts_utc TEXT,
        mode TEXT,
        symbol TEXT,
        side TEXT,
        action TEXT,
        price REAL,
        qty REAL,
        notional REAL,
        reason TEXT,
        extra TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS positions(
        signal_id INTEGER PRIMARY KEY,
        symbol TEXT,
        side TEXT,
        entry_price REAL,
        qty_initial REAL,
        qty_open REAL,
        notional REAL,
        sl REAL,
        tp1 REAL,
        tp2 REAL,
        tp3 REAL,
        tp_stage INTEGER,
        status TEXT,
        opened_at TEXT,
        updated_at TEXT,
        closed_at TEXT,
        exit_price REAL,
        realized_pnl REAL DEFAULT 0
    )""")
    for table, cols in {
        "signals": [
            ("source_channel", "TEXT"),
            ("ts_utc", "TEXT"),
            ("symbol", "TEXT"),
            ("side", "TEXT"),
            ("leverage", "INTEGER"),
            ("entry_low", "REAL"),
            ("entry_high", "REAL"),
            ("tp1", "REAL"),
            ("tp2", "REAL"),
            ("tp3", "REAL"),
            ("sl", "REAL"),
            ("raw_text", "TEXT"),
            ("received_at", "TEXT"),
        ],
        "orders": [
            ("signal_id", "INTEGER"),
            ("ts_utc", "TEXT"),
            ("mode", "TEXT"),
            ("symbol", "TEXT"),
            ("side", "TEXT"),
            ("action", "TEXT"),
            ("price", "REAL"),
            ("qty", "REAL"),
            ("notional", "REAL"),
            ("reason", "TEXT"),
            ("extra", "TEXT"),
        ],
        "positions": [
            ("symbol", "TEXT"),
            ("side", "TEXT"),
            ("entry_price", "REAL"),
            ("entry_low", "REAL"),
            ("entry_high", "REAL"),
            ("qty_initial", "REAL"),
            ("qty_open", "REAL"),
            ("notional", "REAL"),
            ("sl", "REAL"),
            ("tp1", "REAL"),
            ("tp2", "REAL"),
            ("tp3", "REAL"),
            ("tp_stage", "INTEGER DEFAULT 0"),
            ("status", "TEXT"),
            ("opened_at", "TEXT"),
            ("updated_at", "TEXT"),
            ("closed_at", "TEXT"),
            ("exit_price", "REAL"),
            ("realized_pnl", "REAL DEFAULT 0"),
            ("entry_policy", "TEXT"),
            ("pending_created_at", "TEXT"),
            ("pending_expires_at", "TEXT"),
            ("dca_count", "INTEGER DEFAULT 0"),
            ("dca_filled", "INTEGER DEFAULT 0"),
            ("dca_levels_json", "TEXT"),
            ("dca_adds_json", "TEXT"),
            ("strategy_mode", "TEXT"),
            ("v21_enabled", "INTEGER DEFAULT 0"),
            ("v21_config_path", "TEXT"),
            ("v21_strategy_class", "TEXT"),
            ("v21_state_json", "TEXT"),
            ("delegated_capital", "REAL"),
        ],
    }.items():
        for column, ddl in cols:
            ensure_column(cur, table, column, ddl)
    con.commit()
    con.close()


def signal_exists(db_path: Path, message_id: int) -> bool:
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("SELECT 1 FROM signals WHERE telegram_message_id=?", (message_id,))
    ok = cur.fetchone() is not None
    con.close()
    return ok


def signal_tps(sig: Dict[str, Any]) -> List[float]:
    tps = [float(x) for x in sig["tp"][:3]]
    if len(tps) < 3:
        raise ValueError("signal has fewer than three take-profit levels")
    return tps


def load_dca_policy(path: str, side: str, dca_count: int) -> Dict[str, Any]:
    if dca_count <= 0:
        return {"steps": [], "adds": [], "base_weight": 1.0}
    default_steps = [0.3, 0.35, 0.6, 0.8, 0.8] if side == "long" else [0.1, 0.4, 0.6, 0.8, 0.8]
    default_mults = [1.2, 1.0, 1.5, 3.5]
    if yaml is not None and path and Path(path).exists():
        try:
            cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
            params = cfg.get("strategy_params_%s" % side, {}) or {}
            if side == "long":
                default_steps = [float(params.get(k, v)) for k, v in zip(["drop1", "drop2", "drop3", "drop4", "drop5"], default_steps)]
            else:
                default_steps = [float(params.get(k, v)) for k, v in zip(["rise1", "rise2", "rise3", "rise4", "rise5"], default_steps)]
            default_mults = [
                float(params.get("mult2", default_mults[0])),
                float(params.get("mult3", default_mults[1])),
                float(params.get("mult4", default_mults[2])),
                float(params.get("mult5", default_mults[3])),
            ]
        except Exception as exc:
            print("[paper-live] DCA config load failed, using defaults: %s" % exc, flush=True)
    steps = list(default_steps[:dca_count])
    while len(steps) < dca_count:
        steps.append(steps[-1] if steps else 0.5)
    mults = list(default_mults[:dca_count])
    while len(mults) < dca_count:
        mults.append(1.0)
    return {"steps": steps, "adds": mults, "base_weight": 1.0}


def build_dca_plan(side: str, entry_price: float, max_notional: float, dca_count: int, cfg_path: str) -> Dict[str, Any]:
    policy = load_dca_policy(cfg_path, side, dca_count)
    weights = [float(policy["base_weight"])] + [float(x) for x in policy["adds"][:dca_count]]
    scale = float(max_notional) / max(sum(weights), 1e-12)
    base_notional = weights[0] * scale
    add_notionals = [w * scale for w in weights[1:]]
    levels: List[float] = []
    last = float(entry_price)
    for step in policy["steps"][:dca_count]:
        if side == "long":
            last *= 1.0 - float(step) / 100.0
        else:
            last *= 1.0 + float(step) / 100.0
        levels.append(last)
    return {"base_notional": base_notional, "levels": levels, "adds": add_notionals}


def build_v21_entry(symbol: str, side: str, price: float, delegated_capital: float, cfg_path: str) -> Dict[str, Any]:
    cfg = load_one_leg_config(cfg_path, side, delegated_capital)
    strategy = v21_make_strategy(cfg, side)
    row = v21_make_bar(symbol, price, utc_now())
    opened = v21_open_external_signal(strategy, symbol, row)
    meta = cfg["telegram_v21_wrapper"]
    return {
        "cfg": cfg,
        "strategy_class": meta["active_strategy_class"],
        "entry_price": float(opened["entry_price"]),
        "qty": float(opened["qty"]),
        "notional": float(opened["qty"]) * float(opened["entry_price"]),
        "state_json": v21_dumps_state(opened["state"]),
        "tp": opened.get("tp"),
        "reason": opened.get("reason"),
    }


def insert_signal(cur: sqlite3.Cursor, sig: Dict[str, Any], message_id: int, now: str) -> None:
    tps = signal_tps(sig)
    cur.execute("""INSERT INTO signals (
        telegram_message_id, source_channel, ts_utc, symbol, side, leverage,
        entry_low, entry_high, tp1, tp2, tp3, sl, raw_text, received_at
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        message_id,
        sig.get("source_channel", ""),
        sig.get("ts_utc"),
        sig.get("symbol"),
        str(sig["side"]).lower(),
        int(sig.get("leverage_claimed") or 0),
        float(sig["entry_low"]),
        float(sig["entry_high"]),
        tps[0],
        tps[1],
        tps[2],
        float(sig["sl"]),
        sig.get("raw_text", ""),
        now,
    ))


def insert_signal_and_position(
    db_path: Path,
    sig: Dict[str, Any],
    message_id: int,
    notional: float,
    entry_policy: str,
    ticker_price: Optional[float],
    dca_count: int,
    dca_config: str,
    strategy_mode: str,
) -> str:
    if signal_exists(db_path, message_id):
        return "duplicate"
    entry_mid = (float(sig["entry_low"]) + float(sig["entry_high"])) / 2.0
    entry_price = float(ticker_price) if entry_policy == "ticker" and ticker_price else entry_mid
    side = str(sig["side"]).lower()
    v21_entry: Optional[Dict[str, Any]] = None
    if strategy_mode == "v21":
        v21_entry = build_v21_entry(str(sig.get("symbol")), side, entry_price, notional, dca_config)
        entry_price = float(v21_entry["entry_price"])
        entry_notional = float(v21_entry["notional"])
        qty = float(v21_entry["qty"])
        dca_plan = {"levels": [], "adds": []}
    else:
        dca_plan = build_dca_plan(side, entry_price, notional, dca_count, dca_config)
        entry_notional = float(dca_plan["base_notional"])
        qty = entry_notional / entry_price if entry_price > 0 else 0.0
    now = utc_now()
    tps = signal_tps(sig)
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    insert_signal(cur, sig, message_id, now)
    cur.execute("""INSERT INTO orders (
        order_id, signal_id, ts_utc, mode, symbol, side, action, price, qty,
        notional, reason, extra
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (
        str(uuid.uuid4()),
        message_id,
        now,
        "paper_telegram",
        sig.get("symbol"),
        side,
        "open",
        entry_price,
        qty,
        entry_notional,
        f"telegram_signal_{message_id}",
        json.dumps({"entry_policy": entry_policy, "delegated_capital": notional, "dca_count": dca_count, "strategy_mode": strategy_mode}, ensure_ascii=False),
    ))
    cur.execute("""INSERT INTO positions (
        signal_id, symbol, side, entry_price, entry_low, entry_high,
        qty_initial, qty_open, notional, sl, tp1, tp2, tp3, tp_stage, status,
        opened_at, updated_at, closed_at, exit_price, realized_pnl,
        entry_policy, pending_created_at, pending_expires_at,
        dca_count, dca_filled, dca_levels_json, dca_adds_json,
        strategy_mode, v21_enabled, v21_config_path, v21_strategy_class, v21_state_json,
        delegated_capital
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        message_id,
        sig.get("symbol"),
        side,
        entry_price,
        float(sig["entry_low"]),
        float(sig["entry_high"]),
        qty,
        qty,
        entry_notional,
        float(sig["sl"]),
        tps[0],
        tps[1],
        tps[2],
        0,
        "open",
        now,
        now,
        None,
        None,
        0.0,
        entry_policy,
        None,
        None,
        int(dca_count),
        0,
        json.dumps(dca_plan["levels"], ensure_ascii=False),
        json.dumps(dca_plan["adds"], ensure_ascii=False),
        strategy_mode,
        1 if strategy_mode == "v21" else 0,
        dca_config if strategy_mode == "v21" else None,
        v21_entry["strategy_class"] if v21_entry else None,
        v21_entry["state_json"] if v21_entry else None,
        float(notional),
    ))
    con.commit()
    con.close()
    return "open"


def insert_signal_and_pending(db_path: Path, sig: Dict[str, Any], message_id: int, notional: float, entry_timeout_sec: float, dca_count: int, strategy_mode: str, dca_config: str) -> str:
    if signal_exists(db_path, message_id):
        return "duplicate"
    now_dt = dt.datetime.now(dt.timezone.utc)
    now = now_dt.isoformat()
    expires_at = (now_dt + dt.timedelta(seconds=float(entry_timeout_sec))).isoformat()
    side = str(sig["side"]).lower()
    tps = signal_tps(sig)
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    insert_signal(cur, sig, message_id, now)
    cur.execute("""INSERT INTO orders (
        order_id, signal_id, ts_utc, mode, symbol, side, action, price, qty,
        notional, reason, extra
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (
        str(uuid.uuid4()),
        message_id,
        now,
        "paper_telegram",
        sig.get("symbol"),
        side,
        "pending",
        None,
        0.0,
        notional,
        f"telegram_signal_{message_id}",
        json.dumps({
            "entry_policy": "touch",
            "entry_low": float(sig["entry_low"]),
            "entry_high": float(sig["entry_high"]),
            "pending_expires_at": expires_at,
            "max_notional": notional,
            "dca_count": dca_count,
            "strategy_mode": strategy_mode,
        }, ensure_ascii=False),
    ))
    cur.execute("""INSERT INTO positions (
        signal_id, symbol, side, entry_price, entry_low, entry_high,
        qty_initial, qty_open, notional, sl, tp1, tp2, tp3, tp_stage, status,
        opened_at, updated_at, closed_at, exit_price, realized_pnl,
        entry_policy, pending_created_at, pending_expires_at,
        dca_count, dca_filled, dca_levels_json, dca_adds_json,
        strategy_mode, v21_enabled, v21_config_path, v21_strategy_class, v21_state_json,
        delegated_capital
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        message_id,
        sig.get("symbol"),
        side,
        None,
        float(sig["entry_low"]),
        float(sig["entry_high"]),
        0.0,
        0.0,
        notional,
        float(sig["sl"]),
        tps[0],
        tps[1],
        tps[2],
        0,
        "pending",
        None,
        now,
        None,
        None,
        0.0,
        "touch",
        now,
        expires_at,
        int(dca_count),
        0,
        "[]",
        "[]",
        strategy_mode,
        0,
        dca_config if strategy_mode == "v21" else None,
        None,
        None,
        float(notional),
    ))
    con.commit()
    con.close()
    return "pending"


def price_touches_entry(pos: sqlite3.Row, price: float) -> bool:
    low = float(pos["entry_low"])
    high = float(pos["entry_high"])
    return low <= float(price) <= high


def open_pending_position(db_path: Path, pos: sqlite3.Row, price: float, dca_config: str) -> None:
    now = utc_now()
    max_notional = float(pos["notional"])
    dca_count = int(pos["dca_count"] or 0)
    strategy_mode = str(pos["strategy_mode"] or "legacy_dca")
    v21_entry: Optional[Dict[str, Any]] = None
    if strategy_mode == "v21":
        v21_entry = build_v21_entry(str(pos["symbol"]), str(pos["side"]), price, max_notional, dca_config)
        notional = float(v21_entry["notional"])
        qty = float(v21_entry["qty"])
        dca_plan = {"levels": [], "adds": []}
    else:
        dca_plan = build_dca_plan(str(pos["side"]), price, max_notional, dca_count, dca_config)
        notional = float(dca_plan["base_notional"])
        qty = notional / price if price > 0 else 0.0
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("""INSERT INTO orders (
        order_id, signal_id, ts_utc, mode, symbol, side, action, price, qty,
        notional, reason, extra
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (
        str(uuid.uuid4()),
        int(pos["signal_id"]),
        now,
        "paper_telegram",
        pos["symbol"],
        pos["side"],
        "open",
        price,
        qty,
        notional,
        "entry_touch",
        json.dumps({
            "entry_low": float(pos["entry_low"]),
            "entry_high": float(pos["entry_high"]),
            "max_notional": max_notional,
            "dca_count": dca_count,
            "strategy_mode": strategy_mode,
        }, ensure_ascii=False),
    ))
    cur.execute("""UPDATE positions
        SET entry_price=?, qty_initial=?, qty_open=?, notional=?, status='open',
            opened_at=?, updated_at=?, dca_levels_json=?, dca_adds_json=?,
            v21_enabled=?, v21_strategy_class=?, v21_state_json=?
        WHERE signal_id=? AND status='pending'""", (
        price,
        qty,
        qty,
        notional,
        now,
        now,
        json.dumps(dca_plan["levels"], ensure_ascii=False),
        json.dumps(dca_plan["adds"], ensure_ascii=False),
        1 if strategy_mode == "v21" else 0,
        v21_entry["strategy_class"] if v21_entry else None,
        v21_entry["state_json"] if v21_entry else None,
        int(pos["signal_id"]),
    ))
    con.commit()
    con.close()
    print("[paper-live OPEN] msg=%s %s %s price=%.12g reason=entry_touch" % (
        pos["signal_id"], pos["symbol"], pos["side"], price,
    ), flush=True)


def expire_pending_position(db_path: Path, pos: sqlite3.Row) -> None:
    now = utc_now()
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("""INSERT INTO orders (
        order_id, signal_id, ts_utc, mode, symbol, side, action, price, qty,
        notional, reason, extra
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (
        str(uuid.uuid4()),
        int(pos["signal_id"]),
        now,
        "paper_telegram",
        pos["symbol"],
        pos["side"],
        "expired",
        None,
        0.0,
        float(pos["notional"]),
        "entry_timeout",
        json.dumps({"pending_expires_at": pos["pending_expires_at"]}, ensure_ascii=False),
    ))
    cur.execute("""UPDATE positions
        SET status='expired', updated_at=?, closed_at=?
        WHERE signal_id=? AND status='pending'""", (
        now,
        now,
        int(pos["signal_id"]),
    ))
    con.commit()
    con.close()
    print("[paper-live EXPIRE] msg=%s %s %s entry window elapsed" % (
        pos["signal_id"], pos["symbol"], pos["side"],
    ), flush=True)


def dca_level_touched(pos: sqlite3.Row, price: float, level: float) -> bool:
    side = str(pos["side"])
    return float(price) <= float(level) if side == "long" else float(price) >= float(level)


def apply_dca_fills(db_path: Path, pos: sqlite3.Row, price: float) -> None:
    dca_count = int(pos["dca_count"] or 0)
    filled = int(pos["dca_filled"] or 0)
    if dca_count <= 0 or filled >= dca_count:
        return
    try:
        levels = json.loads(pos["dca_levels_json"] or "[]")
        adds = json.loads(pos["dca_adds_json"] or "[]")
    except Exception:
        return
    entry_price = float(pos["entry_price"] or 0.0)
    qty_open = float(pos["qty_open"] or 0.0)
    current_notional = float(pos["notional"] or 0.0)
    if entry_price <= 0 or qty_open <= 0:
        return
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    now = utc_now()
    changed = False
    while filled < min(dca_count, len(levels), len(adds)):
        level = float(levels[filled])
        if not dca_level_touched(pos, price, level):
            break
        add_notional = float(adds[filled])
        add_qty = add_notional / max(level, 1e-12)
        old_qty = qty_open
        qty_open += add_qty
        current_notional += add_notional
        entry_price = current_notional / max(qty_open, 1e-12)
        filled += 1
        changed = True
        cur.execute("""INSERT INTO orders (
            order_id, signal_id, ts_utc, mode, symbol, side, action, price, qty,
            notional, reason, extra
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (
            str(uuid.uuid4()),
            int(pos["signal_id"]),
            now,
            "paper_telegram_dca",
            pos["symbol"],
            pos["side"],
            "dca",
            level,
            add_qty,
            add_notional,
            "dca_touch",
            json.dumps({"dca_filled": filled, "trigger_price": price, "old_qty": old_qty}, ensure_ascii=False),
        ))
        print("[paper-live DCA] msg=%s %s %s fill=%s level=%.12g add=%.12g avg=%.12g" % (
            pos["signal_id"], pos["symbol"], pos["side"], filled, level, add_notional, entry_price,
        ), flush=True)
    if changed:
        cur.execute("""UPDATE positions
            SET entry_price=?, qty_open=?, notional=?, dca_filled=?, updated_at=?
            WHERE signal_id=?""", (
            entry_price,
            qty_open,
            current_notional,
            filled,
            now,
            int(pos["signal_id"]),
        ))
    con.commit()
    con.close()


def close_or_partial(db_path: Path, pos: sqlite3.Row, price: float, reason: str, qty_frac: float, extra: Optional[Dict[str, Any]] = None) -> None:
    qty_open = float(pos["qty_open"])
    qty = qty_open * qty_frac
    if qty <= 0:
        return
    side = str(pos["side"])
    pnl = qty * (price - float(pos["entry_price"])) if side == "long" else qty * (float(pos["entry_price"]) - price)
    new_qty = qty_open - qty
    status = "closed" if new_qty <= 1e-12 else "open"
    now = utc_now()
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("""INSERT INTO orders (
        order_id, signal_id, ts_utc, mode, symbol, side, action, price, qty,
        notional, reason, extra
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (
        str(uuid.uuid4()),
        int(pos["signal_id"]),
        now,
        "paper_telegram",
        pos["symbol"],
        side,
        "close" if status == "closed" else "partial_close",
        price,
        qty,
        qty * price,
        reason,
        json.dumps(extra or {}, ensure_ascii=False),
    ))
    cur.execute("""UPDATE positions
        SET qty_open=?, status=?, updated_at=?, closed_at=CASE WHEN ?='closed' THEN ? ELSE closed_at END,
            exit_price=CASE WHEN ?='closed' THEN ? ELSE exit_price END,
            realized_pnl=realized_pnl+?,
            tp_stage=CASE WHEN ? LIKE 'tp%' THEN tp_stage+1 ELSE tp_stage END
        WHERE signal_id=?""", (
        max(new_qty, 0.0),
        status,
        now,
        status,
        now,
        status,
        price,
        pnl,
        reason,
        int(pos["signal_id"]),
    ))
    con.commit()
    con.close()
    print("[paper-live %s] msg=%s %s %s price=%.12g qty=%.12g pnl=%.12g status=%s" % (
        reason.upper(), pos["signal_id"], pos["symbol"], side, price, qty, pnl, status,
    ), flush=True)


def update_v21_state(db_path: Path, signal_id: int, state_json: str, entry_price: Optional[float], qty_open: Optional[float], notional: Optional[float]) -> None:
    now = utc_now()
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("""UPDATE positions
        SET v21_state_json=?, entry_price=COALESCE(?, entry_price),
            qty_open=COALESCE(?, qty_open), notional=COALESCE(?, notional), updated_at=?
        WHERE signal_id=?""", (
        state_json,
        entry_price,
        qty_open,
        notional,
        now,
        int(signal_id),
    ))
    con.commit()
    con.close()


def apply_v21_manage(db_path: Path, pos: sqlite3.Row, price: float, dca_config: str) -> None:
    side = str(pos["side"])
    cfg_path = str(pos["v21_config_path"] or dca_config)
    delegated = float(pos["delegated_capital"] or pos["notional"] or 0.0)
    cfg = load_one_leg_config(cfg_path, side, delegated)
    strategy = v21_make_strategy(cfg, side)
    row = v21_make_bar(str(pos["symbol"]), price, utc_now())
    before_qty = float(pos["qty_open"] or 0.0)
    before_notional = float(pos["notional"] or 0.0)
    result = v21_manage_existing_position(strategy, str(pos["symbol"]), row, v21_loads_state(pos["v21_state_json"]))
    event = result.get("event")
    state_json = v21_dumps_state(result["state"])
    if not event:
        update_v21_state(db_path, int(pos["signal_id"]), state_json, None, None, None)
        return
    action = str(event.get("action") or "")
    if action == "DCA":
        qty_added = float(event.get("qty_added") or 0.0)
        notional_added = float(event.get("notional_added") or 0.0)
        if qty_added > 0:
            now = utc_now()
            con = sqlite3.connect(db_path)
            cur = con.cursor()
            cur.execute("""INSERT INTO orders (
                order_id, signal_id, ts_utc, mode, symbol, side, action, price, qty,
                notional, reason, extra
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (
                str(uuid.uuid4()),
                int(pos["signal_id"]),
                now,
                "paper_telegram_v21",
                pos["symbol"],
                side,
                "dca",
                price,
                qty_added,
                notional_added,
                "v21_manage_position",
                json.dumps(event, ensure_ascii=False),
            ))
            cur.execute("""UPDATE positions
                SET entry_price=?, qty_open=?, notional=?, dca_filled=?, updated_at=?, v21_state_json=?
                WHERE signal_id=?""", (
                float(result["state"].get("avg_price") or pos["entry_price"]),
                before_qty + qty_added,
                before_notional + notional_added,
                int(result["state"].get("num_fills") or 1) - 1,
                now,
                state_json,
                int(pos["signal_id"]),
            ))
            con.commit()
            con.close()
            print("[paper-live V21 DCA] msg=%s %s %s qty=%.12g notional=%.12g" % (
                pos["signal_id"], pos["symbol"], side, qty_added, notional_added,
            ), flush=True)
        return
    close_or_partial(db_path, pos, float(event["exit_price"]), "v21_" + str(event.get("reason") or action).lower().replace(" ", "_"), float(event.get("qty_frac") or 1.0), event)
    update_v21_state(
        db_path,
        int(pos["signal_id"]),
        state_json,
        float(result["state"].get("avg_price") or pos["entry_price"]) if result["state"].get("avg_price") else None,
        float(result["state"].get("pos_size") or 0.0),
        float(result["state"].get("pos_value_usdt") or 0.0),
    )


def cancel_pending_for_channel_exit(db_path: Path, pos: sqlite3.Row, exit_message_id: int, raw_text: str) -> None:
    now = utc_now()
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("""INSERT INTO orders (
        order_id, signal_id, ts_utc, mode, symbol, side, action, price, qty,
        notional, reason, extra
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (
        str(uuid.uuid4()),
        int(pos["signal_id"]),
        now,
        "paper_telegram",
        pos["symbol"],
        pos["side"],
        "cancelled",
        None,
        0.0,
        float(pos["notional"]),
        "channel_exit",
        json.dumps({
            "exit_message_id": exit_message_id,
            "raw_text": raw_text,
            "previous_status": "pending",
        }, ensure_ascii=False),
    ))
    cur.execute("""UPDATE positions
        SET status='expired', updated_at=?, closed_at=?
        WHERE signal_id=? AND status='pending'""", (
        now,
        now,
        int(pos["signal_id"]),
    ))
    con.commit()
    con.close()
    print("[paper-live CHANNEL_EXIT] msg=%s exit_msg=%s %s %s pending_cancelled" % (
        pos["signal_id"], exit_message_id, pos["symbol"], pos["side"],
    ), flush=True)


def select_channel_exit_positions(db_path: Path, channel: str, exit_symbol: Optional[str]) -> List[sqlite3.Row]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = list(con.execute("""SELECT p.*
        FROM positions p
        JOIN signals s ON s.telegram_message_id=p.signal_id
        WHERE p.status IN ('open', 'pending') AND s.source_channel=?
        ORDER BY COALESCE(p.opened_at, p.pending_created_at, p.updated_at, '') DESC""", (channel,)))
    con.close()
    if exit_symbol:
        wanted = base_symbol(exit_symbol)
        return [row for row in rows if base_symbol(row["symbol"]) == wanted]
    open_rows = [row for row in rows if row["status"] == "open"]
    if open_rows:
        return [open_rows[0]]
    pending_rows = [row for row in rows if row["status"] == "pending"]
    return pending_rows[:1]


def apply_channel_exit(db_path: Path, channel: str, exit_info: Dict[str, Any], message_id: int, ex: Any) -> int:
    positions = select_channel_exit_positions(db_path, channel, exit_info.get("symbol"))
    if not positions:
        print("[paper-live CHANNEL_EXIT] exit_msg=%s symbol=%s no open/pending position" % (
            message_id, exit_info.get("symbol") or "",
        ), flush=True)
        return 0
    closed = 0
    for pos in positions:
        if pos["status"] == "pending":
            cancel_pending_for_channel_exit(db_path, pos, message_id, str(exit_info.get("raw_text") or ""))
            closed += 1
            continue
        price = fetch_price(ex, str(pos["symbol"])) if ex is not None else None
        price_source = "ticker"
        if price is None:
            entry_price = pos["entry_price"]
            price = float(entry_price) if entry_price is not None else 0.0
            price_source = "entry_fallback"
            print("[paper-live CHANNEL_EXIT] msg=%s exit_msg=%s %s ticker unavailable; using entry fallback %.12g" % (
                pos["signal_id"], message_id, pos["symbol"], price,
            ), flush=True)
        if price <= 0:
            print("[paper-live CHANNEL_EXIT] msg=%s exit_msg=%s %s skipped no usable paper price" % (
                pos["signal_id"], message_id, pos["symbol"],
            ), flush=True)
            continue
        close_or_partial(db_path, pos, float(price), "channel_exit", 1.0, {
            "exit_message_id": message_id,
            "raw_text": exit_info.get("raw_text") or "",
            "price_source": price_source,
        })
        closed += 1
    return closed


def build_exchange():
    if ccxt is None:
        return None
    try:
        ex = ccxt.bingx({"enableRateLimit": True})
        ex.load_markets()
        return ex
    except Exception as exc:
        print("[paper-live] ccxt BingX init failed: %s" % exc, flush=True)
        return None


def fetch_price(ex: Any, symbol: str) -> Optional[float]:
    if ex is None:
        return None
    try:
        ticker = ex.fetch_ticker(symbol)
        px = ticker.get("last") or ticker.get("close")
        return float(px) if px else None
    except Exception:
        return None


async def monitor_paper_state(
    db_path: Path,
    poll_sec: float,
    monitor_pending: bool,
    monitor_exits: bool,
    dca_config: str,
    poll_state: Optional[TelegramPollState] = None,
) -> None:
    ex = build_exchange()
    if ex is None:
        print("[paper-live] ccxt not available; entry fills and TP/SL monitor disabled, pending timeouts still active", flush=True)
    while True:
        con = None
        try:
            if poll_state is not None:
                poll_state.record_monitor_poll()
            con = sqlite3.connect(db_path)
            con.row_factory = sqlite3.Row
            pending = []
            if monitor_pending:
                pending = list(con.execute(
                    "SELECT * FROM positions WHERE status='pending' ORDER BY pending_created_at"
                ))
            rows = []
            if monitor_exits:
                rows = list(con.execute("SELECT * FROM positions WHERE status='open' AND qty_open > 0"))
            con.close()
            con = None
            now = utc_now()
            for pos in pending:
                expires_at = str(pos["pending_expires_at"] or "")
                if expires_at and expires_at <= now:
                    expire_pending_position(db_path, pos)
                    continue
                if ex is None:
                    continue
                price = fetch_price(ex, str(pos["symbol"]))
                if price is None:
                    continue
                if price_touches_entry(pos, price):
                    open_pending_position(db_path, pos, price, dca_config)
            for pos in rows:
                price = fetch_price(ex, str(pos["symbol"]))
                if price is None:
                    continue
                if int(pos["v21_enabled"] or 0) and pos["v21_state_json"]:
                    apply_v21_manage(db_path, pos, price, dca_config)
                    continue
                side = str(pos["side"])
                if side == "long" and price <= float(pos["sl"]):
                    close_or_partial(db_path, pos, price, "sl", 1.0)
                elif side == "short" and price >= float(pos["sl"]):
                    close_or_partial(db_path, pos, price, "sl", 1.0)
                else:
                    apply_dca_fills(db_path, pos, price)
                    stage = int(pos["tp_stage"])
                    if stage >= 3:
                        continue
                    tps = [float(pos["tp1"]), float(pos["tp2"]), float(pos["tp3"])]
                    hit = (side == "long" and price >= tps[stage]) or (side == "short" and price <= tps[stage])
                    if hit:
                        frac = 1.0 / 3.0 if stage < 2 else 1.0
                        close_or_partial(db_path, pos, price, f"tp{stage + 1}", frac)
        except Exception as exc:
            if poll_state is not None:
                poll_state.record_monitor_error(exc)
            print("[paper-live MONITOR_ERROR] %s" % safe_error_text(exc), flush=True)
        finally:
            if con is not None:
                con.close()
        await asyncio.sleep(poll_sec)


async def run(args: argparse.Namespace) -> None:
    env_dir = load_env_file(args.env_file)
    channel = normalize_telegram_channel(args.channel or os.environ.get("TG_CHANNEL") or "https://t.me/darkknighttrade")
    session = resolve_path(args.session or os.environ.get("TG_SESSION", "runs/telegram_paper/darkknighttrade_session"), env_dir)
    out = resolve_path(args.out_jsonl or os.environ.get("TG_SIGNAL_OUT", "runs/telegram_paper/darkknighttrade_signals.jsonl"), Path.cwd())
    db_path = resolve_path(args.db, Path.cwd())
    session.parent.mkdir(parents=True, exist_ok=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    ensure_db(db_path)
    ex = build_exchange() if args.entry_policy == "ticker" else None
    channel_exit_ex = ex or build_exchange()
    client = TelegramClient(str(session), int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"])
    poll_state = TelegramPollState()

    @client.on(events.NewMessage(chats=channel))
    async def handler(event):
        try:
            message_id = int(event.message.id)
            raw_text = event.raw_text or ""
            poll_state.record_message()
            sig = parse_signal_text(raw_text, ts_utc=event.message.date.isoformat() if event.message.date else None)
            if not sig:
                exit_info = parse_channel_exit_text(raw_text)
                if exit_info:
                    poll_state.record_channel_exit()
                    apply_channel_exit(db_path, channel, exit_info, message_id, channel_exit_ex)
                return
            sig["source_channel"] = channel
            sig["telegram_message_id"] = message_id
            sig["telegram_message_date"] = event.message.date.isoformat() if event.message.date else None
            ticker_price = fetch_price(ex, sig["symbol"]) if ex is not None else None
            if args.entry_policy == "touch":
                state = insert_signal_and_pending(db_path, sig, message_id, args.notional, args.entry_timeout_sec, args.dca_count, args.strategy_mode, args.dca_config)
            else:
                state = insert_signal_and_position(
                    db_path,
                    sig,
                    message_id,
                    args.notional,
                    args.entry_policy,
                    ticker_price,
                    args.dca_count,
                    args.dca_config,
                    args.strategy_mode,
                )
            if state != "duplicate":
                poll_state.record_signal()
                with out.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(sig, ensure_ascii=False) + "\n")
                if state == "pending":
                    print("[paper-live PENDING] msg=%s %s %s entry=[%.12g, %.12g] timeout_sec=%.1f" % (
                        message_id,
                        sig["symbol"],
                        sig["side"],
                        float(sig["entry_low"]),
                        float(sig["entry_high"]),
                        float(args.entry_timeout_sec),
                    ), flush=True)
                else:
                    print("[paper-live OPEN] msg=%s %s %s delegated=%.12g policy=%s strategy_mode=%s" % (
                        message_id, sig["symbol"], sig["side"], args.notional, args.entry_policy, args.strategy_mode,
                    ), flush=True)
            else:
                poll_state.record_duplicate()
                print("[paper-live SKIP duplicate] msg=%s" % message_id, flush=True)
        except Exception as exc:
            poll_state.record_handler_error(exc)
            print("[paper-live HANDLER_ERROR] %s" % safe_error_text(exc), flush=True)
            traceback.print_exc()
            raise

    print("[paper-live CONNECTING] channel=%s session=%s" % (channel, session), flush=True)
    try:
        await client.start()
    except Exception as exc:
        poll_state.record_disconnect(exc)
        print("[paper-live CONNECT_ERROR] %s" % safe_error_text(exc), flush=True)
        raise
    if not await client.is_user_authorized():
        raise SystemExit("Telethon user session is not authorized")
    print("[paper-live CONNECTED] connected=%s" % ("yes" if client_connected(client) else "unknown"), flush=True)
    print("[paper-live] listening channel=%s db=%s entry_policy=%s strategy_mode=%s poll_sec=%.1f timeout_sec=%.1f heartbeat_sec=%.1f" % (
        channel, db_path, args.entry_policy, args.strategy_mode, args.poll_sec, args.entry_timeout_sec, args.heartbeat_sec,
    ), flush=True)
    print(format_heartbeat_line(poll_state, client_connected(client)), flush=True)
    asyncio.ensure_future(heartbeat_loop(client, poll_state, args.heartbeat_sec))
    if args.entry_policy == "touch" or args.monitor_exits:
        asyncio.ensure_future(monitor_paper_state(
            db_path,
            args.poll_sec,
            args.entry_policy == "touch",
            bool(args.monitor_exits),
            args.dca_config,
            poll_state,
        ))
    try:
        await client.run_until_disconnected()
    except Exception as exc:
        poll_state.record_disconnect(exc)
        print("[paper-live DISCONNECTED] connected=%s error=%s" % (
            "yes" if client_connected(client) else "no",
            safe_error_text(exc),
        ), flush=True)
        raise
    poll_state.record_disconnect()
    print("[paper-live DISCONNECTED] connected=%s run_until_disconnected returned" % (
        "yes" if client_connected(client) else "no",
    ), flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", default="/var/www/vps2.happyuser.info/top/top_1/.env")
    ap.add_argument("--channel", default="https://t.me/darkknighttrade")
    ap.add_argument("--session", default="")
    ap.add_argument("--out-jsonl", default="runs/telegram_paper/darkknighttrade_signals.jsonl")
    ap.add_argument("--db", default="runs/telegram_paper/paper_live.sqlite")
    ap.add_argument("--notional", type=float, default=100.0)
    ap.add_argument("--entry-policy", choices=["touch", "mid", "ticker"], default="touch")
    ap.add_argument("--entry-timeout-sec", type=float, default=900.0)
    ap.add_argument("--dca-count", type=int, default=0, help="Paper DCA levels to enable. --notional remains planned max notional.")
    ap.add_argument("--dca-config", default=DEFAULT_DCA_CONFIG)
    ap.add_argument("--strategy-mode", choices=["v21", "legacy_dca"], default="v21")
    ap.add_argument("--monitor-exits", action="store_true")
    ap.add_argument("--poll-sec", type=float, default=15.0)
    ap.add_argument("--heartbeat-sec", type=float, default=300.0, help="log daemon/Telegram polling heartbeat every N seconds; 0 disables")
    args = ap.parse_args()
    loop = asyncio.get_event_loop()
    loop.run_until_complete(run(args))


if __name__ == "__main__":
    main()
