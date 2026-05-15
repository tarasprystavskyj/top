# Telegram signal data bundle

Цей bundle додає окремі інструменти поверх проекту, без перезапису існуючих backtester/live runner файлів.

## Що додається

- `universe/telegram_signal_universe_all.txt` — всі 52 символи, знайдені в Telegram-сигналах.
- `universe/telegram_signal_universe_top30.txt` — топ-30 символів за кількістю сигналів.
- `universe/telegram_signal_universe_counts.csv` — частоти символів у чаті.
- `obw_platform/telegram_signal_tools/fetch_futures_ohlcv_npz_v1.py` — збір futures OHLCV прямо в multi-symbol NPZ.
- `obw_platform/telegram_signal_tools/build_fast_multi_npz_from_db_safe_v1.py` — безпечна конвертація SQLite DB -> NPZ.
- `obw_platform/telegram_signal_tools/run_telegram_style_strategy_npz_v1.py` — швидкий бектест signal-style стратегії на NPZ.
- `obw_platform/telegram_signal_tools/telegram_signal_listener_paper.py` — paper-only Telegram listener, який пише parsed JSONL без ордерів.
- `obw_platform/telegram_signal_tools/fetch_telegram_channel_signals.py` — backfill останніх повідомлень Telegram-каналу в parsed JSONL.
- `obw_platform/telegram_signal_tools/telegram_signal_schema.py` — спільний parser/schema validator для Telegram-сигналів.
- `obw_platform/telegram_signal_tools/normalize_telegram_signal_jsonl.py` — конвертація paper JSONL у CSV для replay strategy.
- `obw_platform/telegram_signal_tools/validate_telegram_signal_quality.py` — quality report для JSONL/CSV сигналів.
- `obw_platform/telegram_signal_tools/check_telegram_signal_guardrails.py` — легка перевірка no-live guardrails по summary-файлу.
- `obw_platform/telegram_signal_tools/telegram_signal_paper_live_daemon.py` — paper-only daemon: на свіжий сигнал відкриває simulated paper position у SQLite.

## Розгортання поверх проекту

На сервері:

```bash
cd /var/www/vps2.happyuser.info/top/top_1/
mkdir -p /tmp/telegram_data_bundle
# завантаж zip у /tmp/telegram_data_bundle.zip або скопіюй через scp
unzip -o /tmp/telegram_data_bundle.zip -d /tmp/telegram_data_bundle
rsync -av /tmp/telegram_data_bundle/ ./
chmod +x obw_platform/telegram_signal_tools/*.py
```

Активуй venv:

```bash
cd /var/www/vps2.happyuser.info/top/top_1/
source .venv38/bin/activate
python -V
pip install -U ccxt pandas numpy matplotlib
```

Якщо не хочеш оновлювати `numpy` у старому `.venv38`, тоді не запускай `pip install -U numpy`; достатньо:

```bash
pip install -U ccxt pandas matplotlib
```

## Збір NPZ по Telegram-universe

Рекомендований перший прогін: `3m`, `7200` барів. Це приблизно 15 днів даних на символ.

```bash
cd /var/www/vps2.happyuser.info/top/top_1/
source .venv38/bin/activate
mkdir -p DB runs/telegram_signal_bt

python obw_platform/telegram_signal_tools/fetch_futures_ohlcv_npz_v1.py \
  --exchange bingx \
  --universe-file universe/telegram_signal_universe_all.txt \
  --timeframe 3m \
  --bars 7200 \
  --quotes USDT \
  --min-bars 5000 \
  --sleep-sec 0.12 \
  --out DB/telegram_signals_3m_7200b_bingx.npz \
  2>&1 | tee runs/telegram_signal_bt/fetch_3m_7200b_bingx.log
```

Якщо BingX не віддає частину символів, спробуй Bybit:

```bash
python obw_platform/telegram_signal_tools/fetch_futures_ohlcv_npz_v1.py \
  --exchange bybit \
  --universe-file universe/telegram_signal_universe_all.txt \
  --timeframe 3m \
  --bars 7200 \
  --min-bars 5000 \
  --sleep-sec 0.12 \
  --out DB/telegram_signals_3m_7200b_bybit.npz \
  2>&1 | tee runs/telegram_signal_bt/fetch_3m_7200b_bybit.log
```

Для довшого тесту краще `1m` на 100000+ барів або `5m` на 50000+ барів, але це довше і важче по API:

```bash
python obw_platform/telegram_signal_tools/fetch_futures_ohlcv_npz_v1.py \
  --exchange bingx \
  --universe-file universe/telegram_signal_universe_top30.txt \
  --timeframe 5m \
  --bars 50000 \
  --quotes USDT \
  --min-bars 30000 \
  --sleep-sec 0.15 \
  --out DB/telegram_signals_top30_5m_50000b_bingx.npz \
  2>&1 | tee runs/telegram_signal_bt/fetch_top30_5m_50000b_bingx.log
```

## Бектест strategy-style на NPZ

```bash
python obw_platform/telegram_signal_tools/run_telegram_style_strategy_npz_v1.py \
  --npz DB/telegram_signals_3m_7200b_bingx.npz \
  --universe-file universe/telegram_signal_universe_all.txt \
  --out-dir runs/telegram_signal_bt/bt_3m_7200b_bingx \
  --fee-rate 0.0005 \
  --slip-rate 0.00092387 \
  --risk-pct 0.005 \
  --max-concurrent 8 \
  --max-gross 1.2 \
  --quote-filter USDT \
  2>&1 | tee runs/telegram_signal_bt/bt_3m_7200b_bingx.log
```

Результати будуть тут:

```bash
cat runs/telegram_signal_bt/bt_3m_7200b_bingx/telegram_style_summary.json
ls -lh runs/telegram_signal_bt/bt_3m_7200b_bingx/
```

## Paper ingestion -> normalized replay CSV

Paper listener не торгує. Він тільки парсить повідомлення Telegram і додає JSONL. Цільовий канал для цієї гілки: `https://t.me/darkknighttrade`.

```bash
mkdir -p runs/telegram_paper
export TG_API_ID='YOUR_API_ID'
export TG_API_HASH='YOUR_API_HASH'
export TG_CHANNEL='https://t.me/darkknighttrade'
export TG_SESSION='runs/telegram_paper/darkknighttrade_session'
export TG_SIGNAL_OUT='runs/telegram_paper/darkknighttrade_signals.jsonl'

python obw_platform/telegram_signal_tools/telegram_signal_listener_paper.py
```

`TG_CHANNEL` можна задавати як `darkknighttrade`, `@darkknighttrade` або `https://t.me/darkknighttrade`; listener нормалізує це до Telegram entity name.

Для разового backfill останніх повідомлень каналу після авторизації user session:

```bash
python obw_platform/telegram_signal_tools/fetch_telegram_channel_signals.py \
  --env-file C:/python_scripts/top_1/.env \
  --channel https://t.me/darkknighttrade \
  --limit 500
```

Важливо: bot token не може читати історію каналу через Telegram `get_messages`; потрібна авторизована user session Telethon.

Для paper-live на свіжих сигналах:

```bash
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

Цей daemon не ставить реальні ордери. Він пише simulated `signals`, `orders`, `positions` у SQLite. За замовчуванням `--entry-policy touch` створює pending paper-entry, опитує BingX ticker кожні 15 секунд, відкриває simulated position тільки якщо ціна торкнулась entry zone протягом 900 секунд, інакше позначає pending як `expired`. Для сумісності лишились immediate режими `--entry-policy mid` та `--entry-policy ticker`.

Щоб прогнати ці paper-сигнали через standard replay, нормалізуй JSONL у CSV:

```bash
python obw_platform/telegram_signal_tools/validate_telegram_signal_quality.py \
  --signals runs/telegram_paper/darkknighttrade_signals.jsonl \
  --min-valid-ratio 0.95

python obw_platform/telegram_signal_tools/normalize_telegram_signal_jsonl.py \
  --jsonl runs/telegram_paper/darkknighttrade_signals.jsonl \
  --out-csv telegram_signal_standard_bt/telegram_signals_paper.csv

python obw_platform/telegram_signal_tools/validate_telegram_signal_quality.py \
  --signals telegram_signal_standard_bt/telegram_signals_paper.csv \
  --fail-on-invalid
```

За замовчуванням quality CLI тільки друкує report. Додай `--fail-below-threshold`, якщо потрібно non-zero exit при `valid_ratio < --min-valid-ratio`.

Після цього в `telegram_signal_standard_bt/cfg_telegram_signal_replay_3m.yaml` можна тимчасово замінити `signals_csv` на `telegram_signal_standard_bt/telegram_signals_paper.csv`.

## Конвертація існуючої SQLite DB у NPZ

```bash
python obw_platform/telegram_signal_tools/build_fast_multi_npz_from_db_safe_v1.py \
  --db DB/combined_cache_3m_7200_500u.db \
  --out DB/combined_cache_3m_7200_500u.fast.npz \
  2>&1 | tee runs/telegram_signal_bt/convert_db_to_npz.log
```

Якщо побачиш `DB looks truncated`, це означає, що файл неповний. Не обходь це `--skip-integrity-check`, бо отримаєш фальшивий backtest.

## Sanity policy

Не запускати live по цій логіці, поки немає:

- 100+ угод на довшому періоді;
- PF > 1.3 після fee/slippage;
- MTM DD < 20%;
- тесту на іншій біржі або іншому періоді;
- paper-перевірки реальних Telegram-сигналів з latency/fill logging.

Легка перевірка summary перед будь-яким live/paper-live wiring:

```bash
python obw_platform/telegram_signal_tools/check_telegram_signal_guardrails.py \
  --summary runs/telegram_signal_bt/bt_3m_7200b_bingx/telegram_style_summary.json
```

Скрипт повертає non-zero exit, якщо не виконані мінімальні пороги `trades >= 100`, `profit_factor >= 1.3`, `mtm_max_dd_pct >= -0.20`.
