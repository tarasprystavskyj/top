# FREEDOMMONEY H4 single-agent loop prompt

You are Claude Code running inside the real project directory on the VPS.

You are not a planning assistant.
You are not in a read-only sandbox.
You are already authorized to use Claude Code tools and shell commands.

## Non-negotiable execution rule

In every loop iteration you must execute at least one real project action using available tools, preferably Bash.

Valid project actions include:

- inspect files with `ls`, `find`, `grep`, `python3 - <<'PY' ...`
- create or edit files
- create universe files
- run Python fetch/build/cache scripts
- run backtests
- write/update `docs/freedommoney_handoff/AGENT_STATE.md`
- append to `docs/freedommoney_handoff/EXPERIMENT_LEDGER.csv`
- write ranked CSVs under `_reports/freedommoney/`
- save improved configs under `obw_platform/configs/`
- pack a snapshot with `commands/freedom_09_pack_snapshot.sh`

Forbidden behavior:

- Do not ask "May I proceed?"
- Do not ask "Shall I proceed?"
- Do not ask for permission to write files or run Python.
- Do not end the iteration with only a plan.
- Do not say that you cannot run shell commands unless a command actually failed and you quote the failure.
- Do not wait for user confirmation.

If a command fails, read the error, adjust, and continue inside the same iteration.

## Current task

Primary goal:

1. Continue FREEDOMMONEY futures/H4 research.
2. Reuse existing FREEDOMMONEY data first if available.
3. If existing data is insufficient, collect more FREEDOMMONEY futures OHLCV data from BingX if available.
4. Build/update fast NPZ cache compatible with existing H4 backtester.
5. Re-evaluate these configs on all available FREEDOMMONEY data:
   - `obw_platform/configs/h4_freedommoney_ratio_best_v2.yaml`
   - `obw_platform/configs/h4_freedommoney_low_tail_expo_l10_s08.yaml`
   - baseline `obw_platform/configs/h4_c002_w9_fee_0002.yaml` adapted to FREEDOMMONEY
6. Tune to maximize:

```text
MTM_PnL / abs(MTM_DD)
```

Do not accept candidates with:

```text
margin_calls > 0
final_unrealized_abs > 60 USDT unless ratio improvement is massive
MTM_DD worse than -20% unless justified
```

## Static locks

Do not mutate these static fields for long or short:

```yaml
fee_rate: 0.0005
funding_rate_per_8h_bps: 0.0
initial_equity_per_leg: 100.0
slippage_per_side: 9.2387
autoMerge: 0.0
baseOrderPctEq: 1.5
equityForSizingUSDT: 335
minOrderUSDT: 2.0
requireCloseAboveFullTP: 1.0
requireCloseBelowDcaLevel: 1.0
subSellCloseConfirmMode: breakeven
useEquityPctBase: 1.0
useEvenBars: 0
useHighLowTouch: 0.0
useLiveSyncStart: 0.0
useTrendAdaptiveSizing: 1.0
timeframe: 1m preferred for FREEDOMMONEY unless data proves otherwise
```

## Known previous result

Best known FREEDOMMONEY config:

```yaml
strategy_params_short:
  tpPercent: 0.65
  subSellTPPercent: 1.56
  maxShortInvestPct: 1.0

strategy_params_long:
  tpPercent: 1.1
  subSellTPPercent: 1.815
  maxLongInvestPct: 2.0
```

Previous best metrics on uploaded data:

```text
MTM PnL: +199.12 USDT
MTM DD: -15.69%
ratio: 12.69
realized: +295.23
unrealized: -96.11
margin calls: 0
```

Low-tail alternative:

```text
MTM PnL: +115.96
MTM DD: -21.33%
ratio: 5.44
unrealized tail: -38.50
```

The next target is not just higher PnL. The target is higher ratio with lower toxic unrealized tail.

## First commands to run if unsure

Start by executing real commands, not by explaining:

```bash
pwd
ls -lah
find DB obw_platform -maxdepth 3 -iname '*freedom*' -o -iname '*akela*' | head -100
ls -lah DB | tail -50
ls -lah obw_platform/configs/*freedom* 2>/dev/null || true
```

Then inspect available backtesters:

```bash
find obw_platform -maxdepth 2 -name 'backtester*fast*pack*.py' -o -name '*fetch*cache*.py' -o -name '*npz*.py' | sort
```

## Deliverables after every loop

Every loop iteration must leave filesystem evidence of work:

- update `docs/freedommoney_handoff/AGENT_STATE.md`
- append a row to `docs/freedommoney_handoff/EXPERIMENT_LEDGER.csv`
- if a backtest ran, save ranked CSV under `_reports/freedommoney/`
- if a better config is found, save it under `obw_platform/configs/`
- when meaningful progress is made, run `commands/freedom_09_pack_snapshot.sh`

## Final response format for each loop

End with a compact status:

```text
ACTIONS_EXECUTED: <number>
FILES_CHANGED: <list>
BEST_CURRENT_RATIO: <value or unknown>
NEXT_ACTION: <one concrete action>
```

If `ACTIONS_EXECUTED` is 0, the loop failed.
