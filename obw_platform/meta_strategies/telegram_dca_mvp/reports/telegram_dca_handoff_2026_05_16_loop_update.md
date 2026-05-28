# Telegram DCA Loop Update - 2026-05-16

Mode: research-only, paper backtests only. Do not touch live/daemon/order execution.

## New Handoff Bundle

Source archive:

`C:\Users\1\Downloads\telegram_dca_handoff_2026-05-16.tar.gz`

Extracted under:

`C:\python_scripts\top_1\obw_platform\meta_strategies\telegram_dca_mvp\external_handoffs\telegram_dca_handoff_2026-05-16\handoff_package`

The bundle's key positive result is on a filtered `edge_min5` subset, not on the full Telegram universe.

## Reproduced Positive Subset

Command family:

```bash
python obw_platform/meta_strategies/telegram_dca_mvp/run_telegram_simple_baseline_npz.py \
  --npz DB/telegram_signals_1m_event_windows_720h_bingx.npz \
  --signals-csv obw_platform/meta_strategies/telegram_dca_mvp/reports/telegram_signals_raw_edge_min5.csv \
  --events DB/telegram_signal_standard_bt/telegram_channel_exit_events.csv \
  --out-dir obw_platform/meta_strategies/telegram_dca_mvp/reports/edge68_reproduce_720h_tp3 \
  --entry-mode close_in_zone \
  --signal-ttl-hours 72 \
  --signal-hard-ttl-sec 3600 \
  --exit-at-tp 3 \
  --tp-margin-weights edge_in_zone \
  --move-meta-stop-after-tp
```

Result:

- input rows: 83
- opened trades: 68
- PnL: +9.2276%
- MDD: -1.1122%
- PnL/MDD: 8.2967

This exactly reproduces the handoff result on both the older event-window NPZ and the 720h NPZ.

## Full Universe Control

Same execution protocol on all Telegram signals:

- input rows: 312
- opened trades: 251
- skipped missing: 7
- rejected: 54
- PnL: -16.1257%
- MDD: -20.1267%
- PnL/MDD: -0.8012

Output directory:

`C:\python_scripts\top_1\obw_platform\meta_strategies\telegram_dca_mvp\reports\all_signals_720h_tp3_edge_in_zone_events`

## Selector Audit Snapshot

The current `raw_edge_min5` subset appears to be a symbol-level positive-outcome subset, not a causal rule.

Selected symbols:

```text
AAVE, ADA, APT, DOT, ENS, JUP, NEAR, OP, ORDI
```

Those symbols are mostly the profitable symbols in the full TP3 run. Examples from the full-universe control:

```text
DOT   n=13 pnl=+22.9334
AAVE  n=10 pnl=+13.2811
NEAR  n=7  pnl=+13.0098
ADA   n=9  pnl=+10.8260
JUP   n=6  pnl=+9.4906
ORDI  n=5  pnl=+9.4864
OP    n=7  pnl=+3.6501
APT   n=8  pnl=+2.0667
ENS   n=3  pnl=+1.7510
```

Large negative full-universe symbols excluded by the subset:

```text
PYTH   n=9  pnl=-44.7538
ICP    n=5  pnl=-35.7967
RENDER n=24 pnl=-28.2823
VET    n=5  pnl=-26.3290
SUI    n=16 pnl=-19.9802
LINK   n=9  pnl=-14.1998
JTO    n=6  pnl=-14.0498
INJ    n=12 pnl=-12.5245
```

Implication: `edge_min5` is useful as an oracle/reference subset, but it must be rederived using only at-entry information and validated by time split or walk-forward before any strategy promotion.

## Research Implication

The profitable result is not "TP3 solves execution". It is "the `edge_min5` subset is profitable under TP3 + edge_in_zone + move stop after TP".

The next loop should answer:

1. What exactly defines `edge_min5`, and does it use future information?
2. Can the selector be expressed using only data known at signal time?
3. Does it survive walk-forward or time split validation?
4. Which rejected or losing full-universe signals differ structurally from the 68 positive subset?
5. Can the selector be expanded while keeping positive PnL/MDD and at least 50 opened trades?

## Loop Gate

Do not promote DCA yet. First validate the no-DCA selector/filter logic. DCA may only be compared after a defensible no-DCA subset exists.
