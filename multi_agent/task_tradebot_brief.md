Ось мінімальне завдання для агентів (ітераційний цикл):

1) Запустити бектест:
   - якщо ти у папці backtest_SK: `python3 backtester_core.py --cfg configs/cs_C2_base_1h.yaml`
   - якщо у корені репо: `python3 backtest_SK/backtester_core.py --cfg backtest_SK/configs/cs_C2_base_1h.yaml`

2) Проаналізувати, чи покращилися метрики у `reports/c2_repeat_1h_1440_summary.csv`
   (цікавлять: Equity end, Trades, Profit Factor, Max DD, Win-rate).

3) Змінити один параметр у конфігу `cs_C2_base_1h.yaml`,
   перезапустити бектест та порівняти результати.

Повторювати кроки 1–3 кілька ітерацій.
Формат відповіді: 1) Короткий план, 2) Кроки виконання, 3) Перевірка/валідація, 4) Ризики/обмеження, 5) Висновок (3–5 речень).
