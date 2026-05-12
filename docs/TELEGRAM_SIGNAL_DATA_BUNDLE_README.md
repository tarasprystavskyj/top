# Telegram signal data bundle

Цей bundle додає окремі інструменти поверх проекту, без перезапису існуючих backtester/live runner файлів.

## Що додається

- `universe/telegram_signal_universe_all.txt` — всі 52 символи, знайдені в Telegram-сигналах.
- `universe/telegram_signal_universe_top30.txt` — топ-30 символів за кількістю сигналів.
- `universe/telegram_signal_universe_counts.csv` — частоти символів у чаті.
- `obw_platform/telegram_signal_tools/fetch_futures_ohlcv_npz_v1.py` — збір futures OHLCV прямо в multi-symbol NPZ.
- `obw_platform/telegram_signal_tools/build_fast_multi_npz_from_db_safe_v1.py` — безпечна конвертація SQLite DB -> NPZ.
- `obw_platform/telegram_signal_tools/run_telegram_style_strategy_npz_v1.py` — швидкий бектест signal-style стратегії на NPZ.

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
