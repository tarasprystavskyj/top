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
chmod +x obw_platform/telegram_signal_tools/*.py
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
export TG_CHANNEL='https://t.me/darkknighttrade'
export TG_SESSION='runs/telegram_paper/darkknighttrade_session'
export TG_SIGNAL_OUT='runs/telegram_paper/darkknighttrade_signals.jsonl'

python obw_platform/telegram_signal_tools/telegram_signal_listener_paper.py
```

First run will ask for phone/login code. Run it inside tmux.

```bash
tmux new -s tg_darkknight_paper
```

To detach: `Ctrl+B`, then `D`.

This script does not place orders. It only parses and appends signals to JSONL.
`TG_CHANNEL` may be `darkknighttrade`, `@darkknighttrade`, or `https://t.me/darkknighttrade`.

For a one-shot backfill after the user Telethon session is authorized:

```bash
python obw_platform/telegram_signal_tools/fetch_telegram_channel_signals.py \
  --env-file C:/python_scripts/top_1/.env \
  --channel https://t.me/darkknighttrade \
  --limit 500
```

Bot tokens cannot read channel history through Telegram `get_messages`; this needs a user session.

## Paper-live daemon

```bash
python obw_platform/telegram_signal_tools/telegram_signal_paper_live_daemon.py \
  --env-file /var/www/vps2.happyuser.info/top/top_1/.env \
  --channel https://t.me/darkknighttrade \
  --out-jsonl runs/telegram_paper/darkknighttrade_signals.jsonl \
  --db runs/telegram_paper/paper_live.sqlite \
  --notional 100 \
  --entry-policy mid \
  --monitor-exits
```

This is paper-only. It writes simulated `signals`, `orders`, and `positions` to SQLite and does not place exchange orders.
`--dca-count N` enables V21-style paper DCA. `--notional` remains the planned
maximum position notional, so initial entry is scaled down when DCA is enabled:

```bash
python obw_platform/telegram_signal_tools/telegram_signal_paper_live_daemon.py \
  --env-file /var/www/vps2.happyuser.info/top/top_1/.env \
  --channel https://t.me/Nevskiyh \
  --out-jsonl runs/telegram_paper/Nevskiyh_signals.jsonl \
  --db runs/telegram_paper/Nevskiyh_paper_live.sqlite \
  --notional 100 \
  --entry-policy touch \
  --dca-count 3 \
  --monitor-exits
```

## Paper-live status/equity snapshot

For a runner-style status/equity snapshot without touching Telegram sessions or
placing orders:

```bash
python obw_platform/telegram_signal_tools/telegram_paper_live_status.py \
  --db runs/telegram_paper/paper_live.sqlite \
  --initial-equity 1000 \
  --out-json runs/telegram_paper/paper_live_status.json \
  --write-session-db runs/telegram_paper/session.sqlite
```

Add `--fetch-marks` only when current BingX MTM marks are needed. Without it,
open positions use entry-price fallback, so unrealized PnL is conservative zero.

## Validate signal quality

```bash
python obw_platform/telegram_signal_tools/validate_telegram_signal_quality.py \
  --signals runs/telegram_paper/darkknighttrade_signals.jsonl \
  --min-valid-ratio 0.95
```

By default this command prints a report and exits 0. Add `--fail-below-threshold` when CI or deployment should stop on low `valid_ratio`.

## Normalize paper JSONL for replay

```bash
python obw_platform/telegram_signal_tools/normalize_telegram_signal_jsonl.py \
  --jsonl runs/telegram_paper/darkknighttrade_signals.jsonl \
  --out-csv telegram_signal_standard_bt/telegram_signals_paper.csv
```

Then point `strategy_params.signals_csv` in `telegram_signal_standard_bt/cfg_telegram_signal_replay_3m.yaml` to `telegram_signal_standard_bt/telegram_signals_paper.csv` before running the standard replay.

After normalization, re-run quality validation on the CSV:

```bash
python obw_platform/telegram_signal_tools/validate_telegram_signal_quality.py \
  --signals telegram_signal_standard_bt/telegram_signals_paper.csv \
  --fail-on-invalid
```

## No-live guardrail check

```bash
python obw_platform/telegram_signal_tools/check_telegram_signal_guardrails.py \
  --summary telegram_signal_standard_bt/reports/standard_bt_telegram_replay_summary.csv
```

Default thresholds are at least 100 trades, PF >= 1.3, and MTM DD no worse than -20%.
