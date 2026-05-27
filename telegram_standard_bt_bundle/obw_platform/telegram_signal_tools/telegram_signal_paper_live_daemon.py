#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Paper-live daemon for darkknighttrade Telegram signals.

This script never places real exchange orders. It listens for fresh Telegram
signals, writes them to JSONL and SQLite, and opens simulated paper positions.
If ccxt is installed, it can also monitor BingX ticker prices for TP/SL exits.
"""
import argparse
import asyncio
import datetime as dt
import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from telethon import TelegramClient, events

try:
    import ccxt  # type: ignore
except Exception:
    ccxt = None

try:
    from .telegram_signal_schema import normalize_telegram_channel, parse_signal_text
except ImportError:
    from telegram_signal_schema import normalize_telegram_channel, parse_signal_text


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


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
    con.commit()
    con.close()


def signal_exists(db_path: Path, message_id: int) -> bool:
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("SELECT 1 FROM signals WHERE telegram_message_id=?", (message_id,))
    ok = cur.fetchone() is not None
    con.close()
    return ok


def insert_signal_and_position(db_path: Path, sig: Dict[str, Any], message_id: int, notional: float, entry_policy: str, ticker_price: Optional[float]) -> bool:
    if signal_exists(db_path, message_id):
        return False
    entry_mid = (float(sig["entry_low"]) + float(sig["entry_high"])) / 2.0
    entry_price = float(ticker_price) if entry_policy == "ticker" and ticker_price else entry_mid
    qty = notional / entry_price if entry_price > 0 else 0.0
    now = utc_now()
    side = str(sig["side"]).lower()
    tps = [float(x) for x in sig["tp"][:3]]
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("""INSERT INTO signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        message_id,
        sig.get("source_channel", ""),
        sig.get("ts_utc"),
        sig.get("symbol"),
        side,
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
    cur.execute("""INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (
        str(uuid.uuid4()),
        message_id,
        now,
        "paper_telegram",
        sig.get("symbol"),
        side,
        "open",
        entry_price,
        qty,
        notional,
        f"telegram_signal_{message_id}",
        json.dumps({"entry_policy": entry_policy}, ensure_ascii=False),
    ))
    cur.execute("""INSERT INTO positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        message_id,
        sig.get("symbol"),
        side,
        entry_price,
        qty,
        qty,
        notional,
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
    ))
    con.commit()
    con.close()
    return True


def close_or_partial(db_path: Path, pos: sqlite3.Row, price: float, reason: str, qty_frac: float) -> None:
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
    cur.execute("""INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (
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
        "{}",
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


def build_exchange():
    if ccxt is None:
        return None
    ex = ccxt.bingx({"enableRateLimit": True})
    ex.load_markets()
    return ex


def fetch_price(ex: Any, symbol: str) -> Optional[float]:
    if ex is None:
        return None
    try:
        ticker = ex.fetch_ticker(symbol)
        px = ticker.get("last") or ticker.get("close")
        return float(px) if px else None
    except Exception:
        return None


async def monitor_exits(db_path: Path, poll_sec: float) -> None:
    ex = build_exchange()
    if ex is None:
        print("[paper-live] ccxt not installed; TP/SL monitor disabled", flush=True)
        return
    while True:
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        rows = list(con.execute("SELECT * FROM positions WHERE status='open' AND qty_open > 0"))
        con.close()
        for pos in rows:
            price = fetch_price(ex, str(pos["symbol"]))
            if price is None:
                continue
            side = str(pos["side"])
            if side == "long" and price <= float(pos["sl"]):
                close_or_partial(db_path, pos, price, "sl", 1.0)
            elif side == "short" and price >= float(pos["sl"]):
                close_or_partial(db_path, pos, price, "sl", 1.0)
            else:
                stage = int(pos["tp_stage"])
                if stage >= 3:
                    continue
                tps = [float(pos["tp1"]), float(pos["tp2"]), float(pos["tp3"])]
                hit = (side == "long" and price >= tps[stage]) or (side == "short" and price <= tps[stage])
                if hit:
                    frac = 1.0 / 3.0 if stage < 2 else 1.0
                    close_or_partial(db_path, pos, price, f"tp{stage + 1}", frac)
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
    client = TelegramClient(str(session), int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"])

    @client.on(events.NewMessage(chats=channel))
    async def handler(event):
        sig = parse_signal_text(event.raw_text or "", ts_utc=event.message.date.isoformat() if event.message.date else None)
        if not sig:
            return
        message_id = int(event.message.id)
        sig["source_channel"] = channel
        sig["telegram_message_id"] = message_id
        sig["telegram_message_date"] = event.message.date.isoformat() if event.message.date else None
        ticker_price = fetch_price(ex, sig["symbol"]) if ex is not None else None
        opened = insert_signal_and_position(db_path, sig, message_id, args.notional, args.entry_policy, ticker_price)
        if opened:
            with out.open("a", encoding="utf-8") as f:
                f.write(json.dumps(sig, ensure_ascii=False) + "\n")
            print(f"[paper-live OPEN] msg={message_id} {sig['symbol']} {sig['side']} notional={args.notional}", flush=True)
        else:
            print(f"[paper-live SKIP duplicate] msg={message_id}", flush=True)

    await client.start()
    if not await client.is_user_authorized():
        raise SystemExit("Telethon user session is not authorized")
    print(f"[paper-live] listening channel={channel} db={db_path}", flush=True)
    if args.monitor_exits:
        asyncio.create_task(monitor_exits(db_path, args.poll_sec))
    await client.run_until_disconnected()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", default="/var/www/vps2.happyuser.info/top/top_1/.env")
    ap.add_argument("--channel", default="https://t.me/darkknighttrade")
    ap.add_argument("--session", default="")
    ap.add_argument("--out-jsonl", default="runs/telegram_paper/darkknighttrade_signals.jsonl")
    ap.add_argument("--db", default="runs/telegram_paper/paper_live.sqlite")
    ap.add_argument("--notional", type=float, default=100.0)
    ap.add_argument("--entry-policy", choices=["mid", "ticker"], default="mid")
    ap.add_argument("--monitor-exits", action="store_true")
    ap.add_argument("--poll-sec", type=float, default=15.0)
    args = ap.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
