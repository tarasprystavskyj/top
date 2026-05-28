# HYPE ie500 Champion Paper-Live Restart Handoff

Date: 2026-05-25
Repo: `C:\python_scripts\top_1`

## Local Safety Decision

- No live orders were placed.
- No `.env` or secret files were read.
- No active process was stopped or restarted.
- No pull/reset/revert was run.
- Existing tracked dirty code changes were left unstaged because they may belong to other work:
  - `obw_platform/meta_strategies/telegram_signal_dca/replay_full_v21_external_signals.py`
  - `obw_platform/meta_strategies/telegram_signal_dca/test_telegram_v21_one_leg_wrapper_smoke.py`
  - `obw_platform/meta_strategies/v21_external_signal_wrapper.py`
  - `obw_platform/telegram_signal_tools/telegram_v21_one_leg_wrapper.py`
- Untracked unrelated local scripts/universe files were left unstaged.

## Champion Artifacts Prepared

Primary Pine artifacts:

- `obw_platform/strategies/pine/C - LONG - MA driven HYPE ie500 fixed champion.pine`
- `obw_platform/strategies/pine/C - LONG - MA driven HYPE ie500 fixed champion - copy.pine` equivalent local filename may contain Ukrainian copy suffix.
- `obw_platform/strategies/pine/C - LONG - MA driven 1.pine`

Research reports and reproducibility artifacts:

- `REPORT.md`
- `PACKAGE_MANIFEST_SIGNAL_BOOST.md`
- `WORKER_TASK_STATIC_SIGNALS_PINE.md`
- `TASK_SIGNAL_BOOST_SIZING_UA.md`
- `tv_hype_ie500_copy_local_backtest_90d/REPORT.md`
- `tv_hype_ie500_copy_local_backtest_90d/config.json`
- `tv_hype_ie500_copy_local_backtest_90d/orders.csv`
- `tv_hype_ie500_copy_local_backtest_90d/strategy.pine`
- `tv_hype_ie500_copy_local_backtest_90d/summary.json`
- `signal_dca_variant_sweep_90d/REPORT.md`
- `signal_dca_variant_sweep_90d/summary.csv`
- `signal_dca_variant_sweep_90d/summary.json`
- `signal_dca_variant_sweep_90d/signal_dca_variant_sweep_90d.py`
- `signal_boost_consilium_121d/REPORT.md`
- `signal_boost_consilium_121d/STATUS.json`
- `signal_boost_consilium_121d/summary.csv`
- `signal_boost_consilium_121d/summary.json`
- `signal_boost_consilium_121d/run.log`
- `signal_boost_consilium_121d/run.err.log`
- `signal_boost_consilium_loop_121d.py`
- `signal_boost_consilium_wide_121d/STATUS.json`
- `signal_boost_consilium_wide_121d/signal_boost_consilium_wide_121d.py`

Large omitted artifact:

- `tv_hype_ie500_copy_local_backtest_90d/equity_curve.csv` is about 20 MB and is intentionally not required for deploy/restart.

## Current Best Research Result

Do not promote as live trading.

- 90d TV-copy local baseline: net `113.088815%`, max DD `-19.889437%`.
- Best signal-aware TP 90d sweep: net `119.908969%`, max DD `-19.712136%`.
- 121d signal-boost consilium best: net `148.996524%`, max DD `-31.389586%`.
- 121d wide loop best by net: net `157.271060%`, max DD `-31.389586%`.
- 200% target was not reached.

Gate status:

- Paper/live restart: allowed only as paper-only observation if owner approves.
- Real live orders: blocked.
- Required before live promotion: executable BingX paper-live telemetry parity, reviewed paper-live log, no hidden live-order path, and explicit owner approval.

## Server Target

Server path from existing handoff:

```bash
/var/www/vps2.happyuser.info/top/top_1
```

Known paper-live daemon from existing handoff:

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

This daemon is paper-only according to `docs/TELEGRAM_PAPER_LIVE_SERVER_HANDOFF.md`: it writes simulated `signals`, `orders`, and `positions` to SQLite and JSONL. It still requires `.env` and an authorized Telethon session on the server.

## Safe Server Handoff Commands

Run on server only. Do not run from local Windows.

1. Identify exact paper-live session/process first:

```bash
cd /var/www/vps2.happyuser.info/top/top_1
git status --short --branch
tmux ls || true
pgrep -af 'telegram_signal_paper_live_daemon|paper_live|telegram_paper' || true
```

2. Confirm the tmux session is the expected paper-only daemon:

```bash
tmux capture-pane -pt tg_darkknight_paper_live -S -80 || true
sqlite3 runs/telegram_paper/paper_live.sqlite \
  "select signal_id,symbol,side,entry_price,qty_open,status,opened_at,realized_pnl from positions order by opened_at desc limit 20;"
```

3. Deploy branch only if server worktree is clean or owner has explicitly allowed handling local server changes:

```bash
git fetch origin <BRANCH_NAME>
git checkout <BRANCH_NAME>
```

Do not run `git pull` over a dirty server worktree.

4. Start or restart only after the exact paper-live tmux session is identified.

If no session exists:

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

If session exists and is confirmed to be this paper-only daemon, hand off to the server operator to stop it with Ctrl-C inside tmux and re-run the same command. Do not kill unknown PIDs.

5. Post-restart read-only status check:

```bash
python obw_platform/telegram_signal_tools/telegram_paper_live_status.py \
  --db runs/telegram_paper/paper_live.sqlite \
  --initial-equity 1000 \
  --out-json runs/telegram_paper/paper_live_status.json \
  --write-session-db runs/telegram_paper/session.sqlite

tail -n 20 runs/telegram_paper/darkknighttrade_signals.jsonl
```

## Blockers For This Local Agent

- Server `.env` and Telethon session are required but must not be read locally.
- No local SSH/server access was used.
- Any restart requires confirming the exact server tmux session and command line first.
- The current champion remains research/paper-only; it is not a live-order approval.
