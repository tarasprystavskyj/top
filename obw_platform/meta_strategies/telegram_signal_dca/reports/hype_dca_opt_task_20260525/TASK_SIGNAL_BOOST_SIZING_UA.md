# Завдання для CEX web worker: HYPE ie500 signal+DCA sizing boost

Це research-only пакет для HYPE ie500 / signal+DCA. Не роби live orders, не використовуй secrets, приватні сторінки акаунтів або scraping. Усі висновки мають залишатися в межах backtest/paper research.

## Контекст

- Базовий TV-like DCA за 90 днів дав приблизно +113-114%: `500 -> 1065.44`, net `+113.09%`.
- Найкращий поточний signal-aware TP sweep дав `+119.91%` за 90 днів: `500 -> 1099.54`, `freshness_ms=86400000`, `fresh_tp_percent=1.2`, `fresh_callback_percent=0.25`.
- Поточні sizing boost варіанти поки не обігнали signal-aware TP: найкращий у наявному sweep близько `+111.37%` при `normal_base_pct=16`, `fresh_base_pct=24`, `freshness_ms=21600000`.
- Новий напрям: дослідити signal-boost sizing із нижчим baseline size і сильнішим signal size.

## Що треба дослідити

Знайти конфігурації signal-boost sizing, які можуть наблизити HYPE signal+DCA до цілі `+200%` за `121` день сигналів, без live execution.

Початковий простір:

- baseline/normal base: `8-12%`
- signal/fresh base: `16-24%`
- signal freshness windows: перевірити короткі та довші варіанти, мінімум `2h`, `6h`, `24h`
- можна комбінувати з signal-aware TP, але чітко розділити внесок sizing і TP
- DCA правила не ламати без явного пояснення, що саме змінилось

## Очікуваний результат

1. Розпакуй архів і переглянь наявні Pine/report/script artifacts.
2. Запропонуй короткий bounded smoke plan або, якщо можеш запускати код, запусти тільки research/backtest checks.
3. Порівняй:
   - baseline TV-like DCA
   - signal-aware TP best
   - signal-boost sizing with lower normal base
   - combined signal-aware TP + signal-boost sizing, якщо це доречно
4. Дай таблицю top candidates з return, max drawdown, min total PnL, orders, open exposure.
5. Окремо напиши fail gates: що вважати overfit/replay artifact, які варіанти відкинути.

Ціль агресивна (`+200%/121d`) і не є дозволом на live trading. Потрібна практична оцінка, чи є шлях до неї через signal sizing, чи поточний edge вичерпаний.
