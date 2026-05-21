# Telegram paper-live server handoff

Target server path:

```bash
/var/www/vps2.happyuser.info/top/top_1
```

Target channel:

```text
https://t.me/darkknighttrade
```

Goal: run paper-only live handling for fresh Telegram signals. No real exchange orders.

## Current local status

- Local branch: `feature/telegram-signals-infra`
- Base commit: `904b01e5`
- Local Telethon login succeeded for the user account.
- Backfill from `darkknighttrade` works locally.
- Last local backfill checked 1000 channel messages and parsed 116 valid signals.
- Quality report on that JSONL/CSV: 116/116 valid, 36 symbols, 48 short, 68 long, 0 duplicates.

## Files to deploy

Deploy at least:

```text
obw_platform/telegram_signal_tools/telegram_signal_schema.py
obw_platform/telegram_signal_tools/telegram_signal_listener_paper.py
obw_platform/telegram_signal_tools/fetch_telegram_channel_signals.py
obw_platform/telegram_signal_tools/normalize_telegram_signal_jsonl.py
obw_platform/telegram_signal_tools/validate_telegram_signal_quality.py
obw_platform/telegram_signal_tools/check_telegram_signal_guardrails.py
obw_platform/telegram_signal_tools/telegram_signal_paper_live_daemon.py
docs/TELEGRAM_SIGNAL_DATA_BUNDLE_README.md
docs/TELEGRAM_PAPER_LIVE_SERVER_HANDOFF.md
```

The bundle mirror under `telegram_standard_bt_bundle/` is also updated.

## Server setup

```bash
cd /var/www/vps2.happyuser.info/top/top_1
source .venv38/bin/activate 2>/dev/null || source .venv/bin/activate
pip install -U telethon ccxt
mkdir -p runs/telegram_paper
```

Check `.env` contains:

```text
TG_API_ID=...
TG_API_HASH=...
TG_CHANNEL=https://t.me/darkknighttrade
TG_SESSION=runs/telegram_paper/darkknighttrade_session
TG_SIGNAL_OUT=runs/telegram_paper/darkknighttrade_signals.jsonl
```

Bot token is not enough for channel history. Telegram bot users cannot call `get_messages` on channel history. This must use an authorized user Telethon session.

## Authorize session on server

Run this once in an interactive terminal/tmux:

```bash
cd /var/www/vps2.happyuser.info/top/top_1
source .venv38/bin/activate 2>/dev/null || source .venv/bin/activate
python obw_platform/telegram_signal_tools/telegram_signal_listener_paper.py
```

Enter phone/code when prompted. Stop it after it prints:

```text
paper listener started for darkknighttrade
```

## Backfill sanity check

```bash
python obw_platform/telegram_signal_tools/fetch_telegram_channel_signals.py \
  --env-file /var/www/vps2.happyuser.info/top/top_1/.env \
  --channel https://t.me/darkknighttrade \
  --out-jsonl runs/telegram_paper/darkknighttrade_signals.jsonl \
  --limit 1000 \
  --replace

python obw_platform/telegram_signal_tools/validate_telegram_signal_quality.py \
  --signals runs/telegram_paper/darkknighttrade_signals.jsonl \
  --fail-on-invalid
```

## Start paper-live daemon

Use tmux first:

```bash
tmux new -s tg_darkknight_paper_live
cd /var/www/vps2.happyuser.info/top/top_1
source .venv38/bin/activate 2>/dev/null || source .venv/bin/activate
python obw_platform/telegram_signal_tools/telegram_signal_paper_live_daemon.py \
  --env-file /var/www/vps2.happyuser.info/top/top_1/.env \
  --channel https://t.me/darkknighttrade \
  --out-jsonl runs/telegram_paper/darkknighttrade_signals.jsonl \
  --db runs/telegram_paper/paper_live.sqlite \
  --notional 100 \
  --entry-policy touch \
  --entry-timeout-sec 900 \
  --poll-sec 15 \
  --monitor-exits
```

Detach: `Ctrl+B`, then `D`.

The daemon:

- listens to all new channel messages;
- parses fresh full signals;
- appends JSONL;
- inserts `signals`, `orders`, and `positions` into `runs/telegram_paper/paper_live.sqlite`;
- creates a pending paper entry by default and polls BingX ticker every 15 seconds;
- opens a simulated paper position only when ticker price touches the signal entry zone before the 900 second entry timeout;
- marks untouched pending entries as `expired` without opening a position;
- for non-signal messages, detects manual channel close/exit instructions such as `закриваю позицію`, `закрываю позицию`, `close TAO`, `exit TAO`, `close position`, and `позицію закрито`;
- when a manual channel exit mentions a symbol, closes/cancels only matching open or pending paper positions; without a symbol, closes the most recent open position for this channel, or the most recent pending position if no open position exists;
- manual channel exits write paper-only `orders` rows with reason `channel_exit`; open positions use current ticker when `ccxt` is available, otherwise they close at the entry-price fallback with `price_source=entry_fallback` in order metadata;
- if `--monitor-exits` and `ccxt` are available, polls BingX ticker and writes simulated TP/SL close orders.

Compatibility modes are still available for controlled checks: `--entry-policy mid` opens immediately at the entry midpoint, and `--entry-policy ticker` opens immediately at current ticker price if available.

## Check paper-live state

```bash
sqlite3 runs/telegram_paper/paper_live.sqlite \
  "select signal_id,symbol,side,entry_price,qty_open,status,opened_at,realized_pnl from positions order by opened_at desc limit 20;"

sqlite3 runs/telegram_paper/paper_live.sqlite \
  "select signal_id,symbol,side,entry_low,entry_high,status,pending_created_at,pending_expires_at from positions where status='pending' order by pending_created_at desc;"

tail -f runs/telegram_paper/darkknighttrade_signals.jsonl
```

Runner-style paper-live status/equity export:

```bash
python obw_platform/telegram_signal_tools/telegram_paper_live_status.py \
  --db runs/telegram_paper/paper_live.sqlite \
  --initial-equity 1000 \
  --out-json runs/telegram_paper/paper_live_status.json \
  --write-session-db runs/telegram_paper/session.sqlite
```

This is read-only against the Telegram paper DB except for the explicit output
files. Use `--fetch-marks` only when current BingX MTM marks are needed;
otherwise open positions use entry-price fallback for zero unrealized PnL.

## Safety

- Do not run `bt_live_paper_runner.py --mode live` for this Telegram flow.
- Do not wire real exchange order placement until the paper-live log has been reviewed.
- Signals usually arrive around 21:00 Kyiv time on weekdays, but keep the daemon running continuously.

## For the server agent

Resume:

```bash
codex resume 019e2219-d625-7791-80c7-56061e0a62a8
```

Ask that agent to:

1. verify deployed files and `.env`;
2. authorize Telethon user session if needed;
3. run backfill quality check;
4. start `telegram_signal_paper_live_daemon.py` in tmux;
5. report the tmux session name, JSONL path, SQLite path, and latest quality report.
