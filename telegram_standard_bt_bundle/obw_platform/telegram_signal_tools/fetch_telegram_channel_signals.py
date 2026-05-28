#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetch recent Telegram channel messages and append parsed signals to JSONL.

Requires an authorized user Telethon session. Bot tokens cannot read channel
history through get_messages.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Dict, Set

from telethon import TelegramClient

try:
    from .telegram_signal_schema import normalize_telegram_channel, parse_signal_text
except ImportError:
    from telegram_signal_schema import normalize_telegram_channel, parse_signal_text


def load_env_file(path: str) -> Dict[str, str]:
    env_path = Path(path)
    loaded: Dict[str, str] = {}
    if not env_path.exists():
        return loaded
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)
            loaded[key] = value
    return loaded


def resolve_path(raw: str, base_dir: Path) -> Path:
    p = Path(raw)
    return p if p.is_absolute() else base_dir / p


def existing_message_ids(path: Path, channel: str) -> Set[int]:
    ids: Set[int] = set()
    if not path.exists():
        return ids
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            if row.get("source_channel") in {"", None, channel} and row.get("telegram_message_id") is not None:
                ids.add(int(row["telegram_message_id"]))
        except Exception:
            continue
    return ids


async def run(args: argparse.Namespace) -> None:
    env_file = Path(args.env_file)
    load_env_file(str(env_file))
    env_dir = env_file.parent if env_file.exists() else Path.cwd()
    channel = normalize_telegram_channel(args.channel or os.environ.get("TG_CHANNEL") or "https://t.me/darkknighttrade")
    session = resolve_path(args.session or os.environ.get("TG_SESSION", "runs/telegram_paper/darkknighttrade_session"), env_dir)
    out = resolve_path(args.out_jsonl or os.environ.get("TG_SIGNAL_OUT", "runs/telegram_paper/darkknighttrade_signals.jsonl"), Path.cwd())
    session.parent.mkdir(parents=True, exist_ok=True)
    out.parent.mkdir(parents=True, exist_ok=True)

    client = TelegramClient(str(session), int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"])
    await client.connect()
    authorized = await client.is_user_authorized()
    if not authorized:
        await client.disconnect()
        print(json.dumps({
            "ok": False,
            "channel": channel,
            "authorized": False,
            "reason": "Telethon user session is not authorized. Run telegram_signal_listener_paper.py once in an interactive terminal to enter phone/code.",
        }, indent=2))
        raise SystemExit(2)

    seen_ids = set() if args.replace else existing_message_ids(out, channel)
    messages = await client.get_messages(channel, limit=args.limit)
    rows = []
    for msg in reversed(messages):
        if msg.id in seen_ids:
            continue
        sig = parse_signal_text(msg.raw_text or "", ts_utc=msg.date.isoformat() if msg.date else None)
        if not sig:
            continue
        sig["source_channel"] = channel
        sig["telegram_message_id"] = msg.id
        sig["telegram_message_date"] = msg.date.isoformat() if msg.date else None
        rows.append(sig)
    mode = "w" if args.replace else "a"
    with out.open(mode, encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    await client.disconnect()
    print(json.dumps({"ok": True, "channel": channel, "messages_checked": len(messages), "signals_written": len(rows), "out_jsonl": str(out)}, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", default=r"C:\python_scripts\top_1\.env")
    ap.add_argument("--channel", default="")
    ap.add_argument("--session", default="")
    ap.add_argument("--out-jsonl", default="")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--replace", action="store_true")
    args = ap.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
