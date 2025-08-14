# OBW Platform — Інструкція по деплою та запуску

Цей гайд описує, як розгорнути середовище для бектестів та грід-пошуку параметрів стратегії з вагами індексів перекупленості.

---

## 1. Підготовка середовища

### 1.1. Вимоги
- **Python 3.9+**
- pip (Python package manager)
- Linux / MacOS / WSL (Windows Subsystem for Linux)  
  *(Windows CMD/Powershell не рекомендую для backtest — можливі проблеми з шляхами)*

---

### 1.2. Клонування репозиторію / копіювання файлів
Середовище складається з:
Ось інструкція по деплою середовища у форматі DEPLOY_GUIDE.md
Я зробив її максимально зрозумілою, щоб можна було швидко розгорнути obw_platform з нуля і запустити тести.

markdown
Copy
Edit
# OBW Platform — Інструкція по деплою та запуску

Цей гайд описує, як розгорнути середовище для бектестів та грід-пошуку параметрів стратегії з вагами індексів перекупленості.

---

## 1. Підготовка середовища

### 1.1. Вимоги
- **Python 3.9+**
- pip (Python package manager)
- Linux / MacOS / WSL (Windows Subsystem for Linux)  
  *(Windows CMD/Powershell не рекомендую для backtest — можливі проблеми з шляхами)*

---

### 1.2. Клонування репозиторію / копіювання файлів
Середовище складається з:
obw_platform/
backtester_core.py
strategies/
ob_weighted.py
obw_chunk_runner.py
combined_cache_1440.db
combined_cache_1440_2h.db
combined_cache_1440_4h.db (опційно)

yaml
Copy
Edit

Скопіюйте ці файли у робочу папку сервера або локальної машини.

---

## 2. Встановлення залежностей

Запустіть:
bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install pandas numpy
(Якщо будуть додані інші стратегії — можуть з’явитись додаткові пакети, наприклад ta або schedule)

3. Структура середовища
obw_platform/backtester_core.py — універсальний модуль бектесту (завантаження кеша, прогін стратегії).

obw_platform/strategies/ob_weighted.py — стратегія "Weighted Overbought" з вагами для RSI, Stoch, MFI, HCP.

obw_chunk_runner.py — грід/свіп параметрів для пошуку оптимальних ваг та фільтрів.

combined_cache_*.db — кешовані історичні дані OHLCV + обсяги, вже підготовлені для backtester.

combined_cache_1440.db — 60d, 1h.

combined_cache_1440_2h.db — 120d, 2h.

combined_cache_1440_4h.db — 240d, 4h.

4. Запуск грід-пошуку
Для базового прогону на 60d (1h, 500 барів):

bash
Copy
Edit
python3 obw_chunk_runner.py
Результати:

bash
Copy
Edit
chunk_grid60_500.csv    # повний грід ваг
chunk_wf120_500.csv     # перевірка top-3 на 120d
chunk_wf240_500.csv     # перевірка top-3 на 240d
sweep_minob_500.csv     # sweep по min_ob
sweep_qv24_500.csv      # sweep по min_qv_24h
sweep_qv1h_500.csv      # sweep по min_qv_1h
sweep_topn_500.csv      # sweep по top_n

5. Як додати свою стратегію
Створіть новий файл у obw_platform/strategies/, наприклад:

bash
Copy
Edit
obw_platform/strategies/my_strategy.py
Клас стратегії повинен реалізовувати метод generate_signals(df) та приймати у __init__ свій конфіг.

Замініть імпорт у obw_chunk_runner.py:

python
Copy
Edit
MY_STRAT = load("strategies.my_strategy", f"{ROOT}/strategies/my_strategy.py").MyStrategy
Запустіть аналогічно:

bash
Copy
Edit
python3 obw_chunk_runner.py
6. Корисні поради
Використовуйте tmux або screen для довгих прогонів:

bash
Copy
Edit
tmux new -s backtest
python3 obw_chunk_runner.py
Для економії часу при великих базах можна зменшити limit_bars у obw_chunk_runner.py.

Якщо хочете швидко протестувати зміни у стратегії — зменшіть параметр weights до кількох комбінацій.

