# Telegram signal standard backtest bundle

Adds a strategy adapter for the existing multi-symbol universe backtester:

`obw_platform/backtester_core_speed3_veto_universe_4_mtm_unrealized_v5.py`

This is not a separate custom backtester. The adapter only implements the standard strategy contract:

- `universe()`
- `rank()`
- `entry_signal()`
- `manage_position()`

## Install over project

```bash
cd /var/www/vps2.happyuser.info/top/top_1/
unzip -o /tmp/telegram_standard_bt_bundle.zip -d /tmp/telegram_standard_bt_bundle
rsync -av /tmp/telegram_standard_bt_bundle/ ./
chmod +x telegram_signal_standard_bt/npz_to_price_indicators_db.py
chmod +x obw_platform/telegram_signal_tools/telegram_signal_listener_paper.py
```

## Convert NPZ to DB for the standard DB-based universe backtester

```bash
cd /var/www/vps2.happyuser.info/top/top_1/
source .venv38/bin/activate

python telegram_signal_standard_bt/npz_to_price_indicators_db.py \
  --npz DB/telegram_signals_3m_7200b_bingx.npz \
  --out-db telegram_signal_standard_bt/telegram_signals_3m_7200b_bingx.db \
  --replace
```

## Run historical Telegram signal replay through standard backtester

```bash
cd /var/www/vps2.happyuser.info/top/top_1/
source .venv38/bin/activate

PYTHONPATH=obw_platform python obw_platform/backtester_core_speed3_veto_universe_4_mtm_unrealized_v5.py \
  --cfg telegram_signal_standard_bt/cfg_telegram_signal_replay_3m.yaml \
  2>&1 | tee telegram_signal_standard_bt/standard_replay_run.log
```

Reports will be written under:

```text
telegram_signal_standard_bt/reports/
```

## Paper Telegram listener

Create Telegram API credentials at my.telegram.org, then:

```bash
cd /var/www/vps2.happyuser.info/top/top_1/
source .venv38/bin/activate
pip install telethon

mkdir -p runs/telegram_paper

export TG_API_ID='YOUR_API_ID'
export TG_API_HASH='YOUR_API_HASH'
export TG_CHANNEL='darkknighttrade'
export TG_SESSION='runs/telegram_paper/darkknight_session'
export TG_SIGNAL_OUT='runs/telegram_paper/darkknight_signals.jsonl'

python obw_platform/telegram_signal_tools/telegram_signal_listener_paper.py
```

First run will ask for phone/login code. Run it inside tmux.

```bash
tmux new -s tg_darkknight_paper
```

To detach: `Ctrl+B`, then `D`.

This script does not place orders. It only parses and appends signals to JSONL.
