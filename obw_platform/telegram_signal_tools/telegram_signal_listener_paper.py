#!/usr/bin/env python3
"""Paper-mode Telegram signal listener skeleton.

Requires: pip install telethon pyyaml
It does NOT place live orders. It appends parsed signals to JSONL.
For live trading, connect this output to your existing runner only after paper validation.
"""
import os, re, json, asyncio, datetime as dt
from pathlib import Path
from telethon import TelegramClient, events

NUM_RE = r'[-+]?\d+(?:[\.,]\d+)?'
SIG_RE = re.compile(r'заходжу\s+в\s+([a-z0-9]{2,30})\s+(long|short)\s+(\d{1,3})x', re.I)
ENTRY_RE = re.compile(r'точка\s+входу\s*[:：]?\s*('+NUM_RE+r')\s*[-–—]\s*('+NUM_RE+r')', re.I)
TP_RE = re.compile(r'тейк[-\s]?профіт\s*[:：]?\s*([^\n]+)', re.I)
SL_RE = re.compile(r'стоп[-\s]?лосс\s*[:：]?\s*('+NUM_RE+r')', re.I)

def f(x): return float(x.replace(',', '.'))

def parse_signal(text):
    low=text.lower()
    sm=SIG_RE.search(low); em=ENTRY_RE.search(low); tm=TP_RE.search(low); slm=SL_RE.search(low)
    if not (sm and em and tm and slm):
        return None
    tps=[f(x) for x in re.findall(NUM_RE, tm.group(1))[:3]]
    if len(tps)<3: return None
    a,b=f(em.group(1)),f(em.group(2))
    return {
        'ts_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
        'symbol': sm.group(1).upper() + '/USDT:USDT',
        'side': sm.group(2).lower(),
        'leverage_claimed': int(sm.group(3)),
        'entry_low': min(a,b), 'entry_high': max(a,b),
        'tp': tps, 'sl': f(slm.group(1)),
        'raw_text': text,
        'mode': 'paper_signal_only'
    }

async def main():
    api_id=int(os.environ['TG_API_ID'])
    api_hash=os.environ['TG_API_HASH']
    channel=os.environ['TG_CHANNEL']
    out=Path(os.environ.get('TG_SIGNAL_OUT','telegram_signals_live.jsonl'))
    client=TelegramClient(os.environ.get('TG_SESSION','signal_listener'), api_id, api_hash)
    @client.on(events.NewMessage(chats=channel))
    async def handler(event):
        sig=parse_signal(event.raw_text or '')
        if sig:
            with out.open('a', encoding='utf-8') as fp:
                fp.write(json.dumps(sig, ensure_ascii=False)+'\n')
            print('PARSED_SIGNAL', sig['symbol'], sig['side'], sig['entry_low'], sig['entry_high'])
    await client.start()
    print('paper listener started for', channel)
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
