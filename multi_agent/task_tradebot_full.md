Загальна ціль зробити бота для торгівлі криптовалютою і допомогти мені заробити гроші. 
Ці гроші я хочу витрачасти на гуманітарні та соціальні проекту в Україні не для підтримки війни а для безпеки та благополуччя мирних людей (жінок, дітей, пенсіонерів), в тому числі військовослужбовців котрі списані зі служби через поранення.

----------------

Для отримання натхнення та віри що це можливо почни із бектесту завідомо виграшної стратегії котра має прибутковість на базі 1440год 
Equity end: 250.24
Trades: 34 ↔ 34
Profit Factor: 2.28
Max DD: −5.8% 
Win-rate: 44.117647%

Що всередині:

combined_cache_1440.db — та сама 1h/60d база, на якій відтворюється результат.

backtest_SK/ — робочий код бек-тестера і стратегій.

backtest_SK/configs/cs_C2_base_1h.yaml — вже вказує cache_db: ../combined_cache_1440.db та open_hour_kyiv: 2, kyiv_offset_hours: 3.

reports/c2_repeat_1h_1440_summary.csv, reports/c2_repeat_1h_1440_trades.csv, reports/c2_repeat_1h_1440_equity.png — еталон.

DEPLOY_GUIDE.md — короткий гайд по встановленню/запуску.

README_REPRO_C2.md — покрокова інструкція “як відтворити”.

Сервісні скрипти:

backtest_SK/run_c2_1h_1440.sh — запускає тест та перевірку.

backtest_SK/verify_c2_result.py — звіряє метрики з еталоном.

run_all.sh — швидкий сценарій (поки містить відтворення 1h).

Як повторити результат (коротко):

unzip obw_c2_repro_pack.zip
cd obw_c2_repro_pack
python3 -m venv venv && source venv/bin/activate
pip install --upgrade pip && pip install pandas numpy pyyaml
cd backtest_SK
bash run_c2_1h_1440.sh
Очікування: summary.csv збігається з еталоном (equity_end=250.242449..., trades=34, PF≈2.289, DD≈−5.807%, WR≈44.12%).

УВАГА - завдання - оптимізуй параметри цього бота для збільшення прибутковості та зменшення DD
