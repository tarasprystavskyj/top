#!/usr/bin/env python3
"""Paper-mode Telegram signal listener skeleton.

Requires: pip install telethon pyyaml
It does NOT place live orders. It appends parsed signals to JSONL.
For live trading, connect this output to your existing runner only after paper validation.
"""
import os, json, asyncio
from pathlib import Path
from telethon import TelegramClient, events

try:
    from .telegram_signal_schema import normalize_telegram_channel, parse_signal_text
except ImportError:
    from telegram_signal_schema import normalize_telegram_channel, parse_signal_text


def parse_signal(text):
    return parse_signal_text(text)


def load_env_file(path='C:/python_scripts/top_1/.env'):
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

async def main():
    load_env_file(os.environ.get('TG_ENV_FILE', 'C:/python_scripts/top_1/.env'))
    api_id=int(os.environ['TG_API_ID'])
    api_hash=os.environ['TG_API_HASH']
    channel=normalize_telegram_channel(os.environ.get('TG_CHANNEL') or os.environ.get('TG_CHANNEL_URL') or 'https://t.me/darkknighttrade')
    out=Path(os.environ.get('TG_SIGNAL_OUT','runs/telegram_paper/darkknighttrade_signals.jsonl'))
    session=Path(os.environ.get('TG_SESSION','runs/telegram_paper/darkknighttrade_session'))
    out.parent.mkdir(parents=True, exist_ok=True)
    session.parent.mkdir(parents=True, exist_ok=True)
    client=TelegramClient(str(session), api_id, api_hash)
    @client.on(events.NewMessage(chats=channel))
    async def handler(event):
        sig=parse_signal(event.raw_text or '')
        if sig:
            sig['source_channel'] = channel
            sig['telegram_message_id'] = getattr(event.message, 'id', None)
            msg_date = getattr(event.message, 'date', None)
            if msg_date is not None:
                sig['telegram_message_date'] = msg_date.isoformat()
            with out.open('a', encoding='utf-8') as fp:
                fp.write(json.dumps(sig, ensure_ascii=False)+'\n')
            print('PARSED_SIGNAL', sig['symbol'], sig['side'], sig['entry_low'], sig['entry_high'])
    await client.start()
    print('paper listener started for', channel)
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
