https://docs.google.com/document/d/1WfJwWztTn9zmyoDrs_Do3-tfDoqcPTG0lZzwAasMdxo/edit?usp=sharing

OBW Platform — Research, Backtest, Tuning & Live
A batteries-included toolkit for building and running crypto breakout strategies with:
fast backtest cores,


automatic hyper-parameter tuning,


universe selection,


cache builders for market data,


paper/live execution runners,


a small UI to inspect results.



0) Quick install
# repo root
python -m pip install --upgrade pip
pip install -r requirements.txt

# (optional) extra utilities some scripts use
pip install requests PyYAML pytz

Create a virtualenv if you prefer:
python -m venv .venv38
source .venv38/bin/activate


1) Environment & credentials
Create .env at repo root:
# Exchange keys (example: BingX futures via CCXT)
BINGX_KEY=...
BINGX_SECRET=...

# If you use other exchanges, add their CCXT keys here too.
# Some runners also read QUOTE=USDT, etc.

Most “live/paper” runners accept --env-file .env.
 Never commit real keys.

2) Data cache — build/update
We work on SQLite caches (e.g. DB/combined_cache_3m_14400_24u.db).
 Use fetch_build_cache_v16.py to build or refresh bars from the exchange via CCXT.
Examples:
# 5m timeframe, last 5000 bars, BingX USDT-M futures,
# write to DB/combined_cache_5m_5000_04.09.db
python3 fetch_build_cache_v16.py \
  -i universe_symbols_bingx.csv -t 5m \
  --back-bars 5000 \
  -o DB/combined_cache_5m_5000_04.09.db \
  --exchange bingx --ccxt-symbol-format usdtm

# 3m timeframe, large daily history (7200 bars), 500 symbols
python3 fetch_build_cache_v16.py \
  -i universe_symbols_bingx.csv -t 3m \
  --back-bars 7200 \
  -o DB/combined_cache_3m_7200_500u.db \
  --exchange bingx --ccxt-symbol-format usdtm

Tips
The input list (-i) is a CSV with exchange symbols.


Re-run the builder to “top-up” your caches before backtests.



3) Backtesting
Core: obw_platform/backtester_core_speed3_veto_universe_2.py
 A fast vectorized backtester with veto filters and universe control.
Key arguments
--cfg <YAML> – strategy & runner params (see below).


--cache_db <DB> – source price/indicator cache.


--symbols-file <file> – restrict to a universe list (optional).


--limit-bars N – keep tests fast & deterministic.


--time-from / --time-to – optional time window.


--plots <dir> – save charts into reports.


Examples
# 3m strategy on 7200 bars using your prepared cache DB
python3 obw_platform/backtester_core_speed3_veto_universe_2.py \
  --cfg obw_platform/configs/cfg_t3m_30d_newbest_full_limit_sl_2.yaml \
  --limit-bars 7200 \
  --cache_db DB/combined_cache_3m_14400_24u.db \
  --plots plots_3m

# Backtest on 5m with explicit symbol universe and long cache
python3 obw_platform/backtester_core_speed3_veto_universe_2.py \
  --cfg obw_platform/configs/cfg_avaai_t5m5000_4.yaml \
  --limit-bars 14400 \
  --symbols-file obw_platform/universe/universe_prof_v2_3m.csv \
  --cache_db DB/combined_cache_5m_25920_30u.db \
  --plots plots_5m


4) Automatic tuning
Driver: obw_platform/auto_tuner_rays2grid_v3_fix.py
 Two-phase tuner: first a wide Rays sweep, then a fine Grid sweep.
 It writes all trials, best configs, aggregates and plots to _reports/_backtest.
Key arguments
--cfg <YAML> – base config used as a template.


--limit-bars – how much history to use per run.


--prefix – tag to separate report folders.


--min-trades, --target-trades – population health checks.


Weights & constraints: --w-pf, --w-dd, --w-mono, --dd-target, etc.


--plan <py file or module:path> – which tuning space to try.


Examples
# Wide+fine sweep on 3m setup
python3 obw_platform/auto_tuner_rays2grid_v3_fix.py \
  --cfg obw_platform/configs/cfg_t3m_30d_newbest_full_limit_sl_2.yaml \
  --limit-bars 3600 \
  --plan tuner_plan_t3m_30d_limit_sl_8h.py \
  --prefix t3m_30d \
  --min-trades 40 --target-trades 220 \
  --w-equity 1.0 --w-pf 12.0 --w-dd 220.0 --w-mono 5.0 --dd-target 0.12 \
  --sleep-sec 1

Universe + tuning in one pass
obw_platform/auto_universe_and_tune.py can score a big symbol pool, write the winners into a new universe file, and launch tuners/backtests over that selection.
python3 obw_platform/auto_universe_and_tune.py \
  --cfg obw_platform/configs/cfg_t3m_30d_newbest_full.yaml \
  --limit-bars 7200 \
  --prefix t5k_auto \
  --trades trades_3m_6000.csv \
  --universe-out obw_platform/universe/universe_prof_v2_3m.txt \
  --plots plots_auto \
  --grid \
  --driver obw_platform/backtester_core_speed3_veto_universe_2.py


5) Paper / Live runner
Runner: obw_platform/bt_live_paper_runner_separated_universe.py
 Executes the same logic live or on paper (API mode), polls the exchange, places entry + laddered TP/SL (from the strategy), and logs to _reports/_live/<run_name>:
session.sqlite – orders, fills, PnL, states.


combined_cache_session.db – “session cache” of bars fetched live.


PNGs & CSVs for live dashboards.


A UI endpoint can read these (see UI).


Key arguments
--mode live|paper


--paper-source api (paper uses exchange API without real orders)


--env-file .env – load keys


--cfg <YAML>


--exchange bingx --symbol-format usdtm


--poll-sec, --bar-delay-sec, --limit_klines


--session-db, --cache-out


--universe-file


--hour-cache load|save – persist 1h caches for warm starts


--heat-report – extra diagnostics


Example
BT_IGNORE_VOLSURGE=1 python3 obw_platform/bt_live_paper_runner_separated_universe.py \
  --mode live --paper-source api \
  --env-file .env \
  --cfg obw_platform/configs/cfg_t3m_30d_newbest_full_limit_sl_2.yaml \
  --exchange bingx --symbol-format usdtm \
  --poll-sec 2 --bar-delay-sec 1 --limit_klines 200 \
  --session-db  _reports/_live/livecfg_cfg_t3m_30d/session.sqlite \
  --cache-out   _reports/_live/livecfg_cfg_t3m_30d/combined_cache_session.db \
  --hour-cache load \
  --universe-file obw_platform/universe/universe_prof_v2_3m.csv \
  --heat-report


6) Configs & where logic lives
Main config (YAML)
 obw_platform/configs/cfg_t3m_30d_newbest_full_limit_sl_2.yaml
global risk/fees/slippage


per-trade notional and exposure caps


heat/filters and HTF options


Strategy params (passed to the strategy class)


live runner options (e.g. partial TP ladder definition if you keep it here)


Live runner helpers
obw_platform/configs/live_runner.py – houses live runner helpers (loading cfg, env, session paths, report writers).


obw_platform/configs/common.py – common constants/utilities shared by tools and UI.


Strategies (where trading rules are)
obw_platform/strategies/breakout_avaai_full_with_universe_7.py
 Your current production strategy.
 It computes entries and also the TP/SL ladder. The live runner simply adds them to the position at open (and then manages BE moves, partial fills, etc.).


obw_platform/strategies/breakout_avaai.py – a simpler baseline breakout.


obw_platform/strategies/base.py – base class: common interface, signal objects, shared helpers.


Engine components
obw_platform/engine/portfolio.py – portfolio sizing, notional allocation across multiple concurrent candidates, risk caps.


obw_platform/engine/data.py – price bars, derived indicators, fast slices for the backtesters.


Universes
obw_platform/universe/universe_prof_v2_3m.csv – curated list used by backtests/live.
 You can generate this from auto_universe_and_tune.



7) Databases and outputs
Cache DBs (read-only in backtests):
 DB/combined_cache_*.db – built with fetch_build_cache_v16.py.


Session DB (live/paper):
 _reports/_live/<run_name>/session.sqlite – fills, TP/SL changes, state machine events.


Live cache:
 _reports/_live/<run_name>/combined_cache_session.db – bars fetched during the run.


Backtest reports:
 _reports/_backtest/... – trades CSV (bt_trades.csv), summary CSV (bt_summary.csv), and plots.



8) UI — quick peek
There’s a tiny Next.js/React UI under UI/ with pages like:
UI/frontend/pages/run.tsx – ad-hoc backtest launcher


(plus a “live results” page in the same app that reads _reports/_live and shows equity/returns histograms, trades tables, etc.)


Start the UI the usual way for your Next.js setup (not covered here).

9) Field guide — what each script does
fetch_build_cache_v16.py
Downloads recent OHLCV (and optional indicators) from CCXT into a SQLite cache, deduplicates and compacts.
 Use it before backtests and live runs to guarantee consistent inputs.
obw_platform/backtester_core_speed3_veto_universe_2.py
Vector backtester with:
universe gating (--symbols-file),


veto filters (e.g., volume/momentum/ATR thresholds),


precise TP/SL/fees/slippage modelling,


CSV/PNG outputs for equity, drawdowns and per-trade stats.


obw_platform/auto_tuner_rays2grid_v3_fix.py
Two-phase auto-tuner:
Rays searches a broad hyper-space to find promising basins,


Grid zooms in for stable, monotonic parameter sets.
 Accepts a plan module (e.g. tuner_plan_t3m_30d_limit_sl_8h.py) that defines the search space and scoring algebra.


obw_platform/auto_universe_and_tune.py
Scores many symbols, writes a profitable universe file, optionally kicks off a tuner/backtester on that filtered set, and saves consolidated plots & CSVs.
obw_platform/bt_live_paper_runner_separated_universe.py
Live/paper executor:
polls exchange on a schedule,


opens/closes positions,


attaches TP/SL ladder computed by the strategy,


moves SL to breakeven when rules demand,


writes all artefacts into _reports/_live/<run_name>.



10) Strategy & runner flow (how things fit)
Strategy (breakout_avaai_full_with_universe_7.py) reads bars/indicators and emits:


EntrySig with side/price,


TP/SL ladder with sizes and trigger prices,


optional “heat”/HTF veto flags.


Backtester simulates those signals over cached bars.


Live runner:


converts ladder into exchange orders (reduceOnly TPs, stop-market SL),


on partial fills, updates quantities,


on TP1 hit, optionally moves SL to BE (your latest patch),


logs everything into session.sqlite.



11) Working recipes (tested patterns)
The following recipes are adapted from your run params file and organized by task. Use them verbatim, then tweak paths/timeframes/limits as needed.
run params
Build cache, then backtest 3m setup
python3 fetch_build_cache_v16.py \
  -i universe_symbols_bingx.csv -t 3m \
  --back-bars 7200 \
  -o DB/combined_cache_3m_14400_24u.db \
  --exchange bingx --ccxt-symbol-format usdtm

python3 obw_platform/backtester_core_speed3_veto_universe_2.py \
  --cfg obw_platform/configs/cfg_t3m_30d_newbest_full_limit_sl_2.yaml \
  --limit-bars 7200 \
  --cache_db DB/combined_cache_3m_14400_24u.db \
  --plots plots_3m

Tune config with rays→grid, 8h wallclock budget
python3 obw_platform/auto_tuner_rays2grid_v3_fix.py \
  --cfg obw_platform/configs/cfg_t3m_30d_newbest_full_limit_sl_2.yaml \
  --limit-bars 3600 \
  --plan tuner_plan_t3m_30d_limit_sl_8h.py \
  --prefix t3m_30d \
  --min-trades 40 --target-trades 220 \
  --w-equity 1.0 --w-pf 12.0 --w-dd 220.0 --w-mono 5.0 --dd-target 0.12 \
  --sleep-sec 1

Run live with a selected universe (paper via API)
BT_IGNORE_VOLSURGE=1 python3 obw_platform/bt_live_paper_runner_separated_universe.py \
  --mode live --paper-source api \
  --env-file .env \
  --cfg obw_platform/configs/cfg_t3m_30d_newbest_full_limit_sl_2.yaml \
  --exchange bingx --symbol-format usdtm \
  --poll-sec 2 --bar-delay-sec 1 --limit_klines 200 \
  --session-db  _reports/_live/livecfg_t3m_30d/session.sqlite \
  --cache-out   _reports/_live/livecfg_t3m_30d/combined_cache_session.db \
  --hour-cache load \
  --universe-file obw_platform/universe/universe_prof_v2_3m.csv \
  --heat-report


12) Troubleshooting
“No trades” in backtest
 Ensure --limit-bars covers your signal’s warm-up and the universe actually matches the cache DB symbols.


Live shows weird dates vs backtest
 Backtest uses cache timestamps (UTC); live charts may render local time. Align with --time-from/--time-to when comparing and confirm the session cache has the latest bars.


Partial TP fills & SL to BE
 The runner logs each attempt:


tp_res ok order_id=...


sl->BE ... or fallback notes.
 If you see order size must be less than available after a partial TP, the fix is already in your runner: it recalculates free qty, waits a short cooldown, and retries with a slightly smaller qty_hint.


Cache holes in live
 If charts are truncated, your combined_cache_session.db may be missing recent bars.
 Refill it with fetch_build_cache_v16.py pointing -o to that session DB and --back-bars to the gap you need.



13) Repo map (key files)
obw_platform/
  configs/
    cfg_t3m_30d_newbest_full_limit_sl_2.yaml  # main tuned 3m config
    live_runner.py                              # live helpers
    common.py                                   # shared constants/tools
  strategies/
    breakout_avaai_full_with_universe_7.py      # production breakout (entries + TP/SL ladder)
    breakout_avaai.py                           # simpler baseline
    base.py                                     # strategy base class
  engine/
    portfolio.py                                # allocation, notional, risk caps
    data.py                                     # data access, indicators
  universe/
    universe_prof_v2_3m.csv                     # curated universe
  backtester_core_speed3_veto_universe_2.py     # core backtester
  auto_tuner_rays2grid_v3_fix.py                # two-phase tuner
  auto_universe_and_tune.py                     # universe + tuner driver
  bt_live_paper_runner_separated_universe.py    # paper/live runner
DB/
  combined_cache_3m_14400_24u.db                # example cache db
UI/
  frontend/pages/run.tsx                        # small backtest launcher
 

14) Conventions
Time: caches and backtests operate in UTC. The UI may display your local time.


Fees/slippage: set realistic values in YAML; live will use exchange fills, backtest uses your constants.


TP/SL ladder: computed by the strategy, applied by the live runner on open. Adjust ladders in the strategy code, not in the runner.



That’s it
With the three pillars—cache → backtest/tune → live—you can iterate quickly and keep live behaviour aligned with what the backtester sees. If you want me to tailor the README to a Docker flow or CI (auto cache refresh + scheduled tuning), say the word.

======================
Web interface run:

Структура така:

UI/frontend — Next.js (в .nvmrc стоїть Node 16)

UI/backend — FastAPI + Uvicorn (Python бекенд, API)

у UI/frontend/.env.local прописано: API_URL=http://127.0.0.1:8001

Тобто веб-інтерфейс = Next.js, а бекенд API = FastAPI на 8001.

Як запускати (швидкий варіант, dev)
1) Запусти API (порт 8001)
cd /var/www/vps2.happyuser.info/top/top_1/UI/backend

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

uvicorn api_main:app --host 0.0.0.0 --port 8001

2) Запусти Next.js (і зроби порт 3001, як ти звик)

У тебе в package.json хардкод на -p 3000, тому найпростіше запускати напряму через npx:

cd /var/www/vps2.happyuser.info/top/top_1/UI/frontend

# Node 16 (бо .nvmrc = 16)
nvm use 16

npm ci

# DEV на 3001
npx next dev -p 3001 -H 0.0.0.0


І тоді відкриваєш:
http://vps2.happyuser.info:3001/

Продакшн запуск (якщо ти так робив раніше)
cd /var/www/vps2.happyuser.info/top/top_1/UI/frontend
nvm use 16
npm ci
npm run build

# PROD на 3001
npx next start -p 3001 -H 0.0.0.0

Якщо хочеш, щоб npm run dev одразу слухав 3001

Зараз у UI/frontend/package.json:

"dev": "next dev -p 3000 -H 0.0.0.0"

"start": "next start -p 3000 -H 0.0.0.0"

Можеш просто змінити 3000 → 3001 в цих двох рядках — і тоді буде:

npm run dev
# або
npm run start

Швидка перевірка “що зараз крутиться”
sudo ss -ltnp | egrep ':3001|:8001'