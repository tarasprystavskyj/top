# Edge Min5 Selector Reconstruction - 2026-05-16

Mode: research-only, paper/backtest artifacts only. No live, daemon, secrets, or order execution touched.

## Current Loop State

The autonomous Telegram consilium loop reached `cycle_000016` and started the web-worker send step at `2026-05-16T16:08:04Z`.

Important caveat: `top_event_watchdog.py` still targets the old overloaded Descartes session `019e2cc6-d53b-7331-8aa8-1f4905b31199`. That session is known to fail with context-window exhaustion. New coordination should use the compact successor context instead of relying on that repair path.

## Reconstructed Selector

`telegram_signals_raw_edge_min5.csv` behaves as a full-sample symbol-outcome selector:

```text
source run: reports/simple_full_49_with_channel_exit/telegram_simple_trades.csv
group key: symbol/base
selection rule inferred from artifacts:
  simple_pnl_by_symbol > 0
  and simple_trade_count_by_symbol >= 5
```

This reproduces the selected symbol list:

```text
DOT, ORDI, JUP, NEAR, AAVE, ADA, ENS, APT, OP
```

The supporting artifact is:

```text
reports/edge_filter_raw_edge_min5_by_symbol.csv
```

with:

```text
DOT   n=14 simple_pnl=+32.9837
ORDI  n=9  simple_pnl=+16.8157
JUP   n=9  simple_pnl=+13.4644
NEAR  n=10 simple_pnl=+11.7868
AAVE  n=11 simple_pnl=+10.2762
ADA   n=10 simple_pnl=+8.3890
ENS   n=5  simple_pnl=+3.6399
APT   n=8  simple_pnl=+1.0375
OP    n=7  simple_pnl=+0.7331
```

## Leakage Classification

This is not a causal signal-time selector.

The selector uses realized full-sample per-symbol PnL from the same historical backtest universe. That means it depends on future outcomes relative to many signals in the file. Treat it as an in-sample oracle/reference subset, not as a deployable filter.

Signal-time-known fields present in the selected CSV:

```text
dt_utc, symbol, side, leverage, entry_a, entry_b, sl, tp1, tp2, tp3,
entry_low, entry_high, entry_mid, zone_pct, sl_extra_pct,
tp1_pct, tp2_pct, tp3_pct
```

The existing audits already show these geometry fields do not materially separate selected from non-selected signals. The separation is mainly symbol/cohort composition.

## Controls

Positive selected subset, TP3 + `edge_in_zone` + move stop after TP:

```text
signals_total = 83
opened_trades = 68
PnL = +9.2276%
MDD = -1.1122%
PnL/MDD = 8.2967
```

Same execution rule on full universe:

```text
signals_total = 312
opened_trades = 251
PnL = -16.1257%
MDD = -20.1267%
PnL/MDD = -0.8012
```

## Next Safe Test

Do not tune DCA against fixed `raw_edge_min5`.

## Walk-Forward Sanity Check

A lightweight prior-only check was run on existing trade CSV artifacts only. No new backtest was launched.

On `reports/simple_full_49_with_channel_exit/telegram_simple_trades.csv`, a causal selector using only prior symbol PnL failed:

```text
all trades: 305, total PnL -211.0993 USDT
symbol prior_sum > 0, min_prior=1: 131 picked, PnL -69.0107
symbol prior_sum > 0, min_prior=2: 103 picked, PnL -41.4555
symbol prior_sum > 0, min_prior=3: 86 picked, PnL -33.6199
symbol prior_sum > 0, min_prior=4: 72 picked, PnL -39.2903
symbol prior_sum > 0, min_prior=5: 59 picked, PnL -58.6379
```

On `reports/all_signals_720h_tp3_edge_in_zone_events/telegram_simple_trades.csv`, the same prior-only selector also failed:

```text
all trades: 251, total PnL -161.2568 USDT
symbol prior_sum > 0, min_prior=1: 75 picked, PnL -32.9429
symbol prior_sum > 0, min_prior=2: 63 picked, PnL -48.3831
symbol prior_sum > 0, min_prior=3: 51 picked, PnL -37.9080
symbol prior_sum > 0, min_prior=4: 41 picked, PnL -45.7820
symbol prior_sum > 0, min_prior=5: 29 picked, PnL -38.3535
```

Implication: the oracle `raw_edge_min5` cannot be justified by a simple walk-forward "trade symbols whose prior cumulative PnL is positive" rule. A stronger causal selector would need additional signal-time or prior-regime features, and must still beat the full-universe negative control.

Next no-DCA task:

1. Build a walk-forward symbol selector where each signal can only use prior trades for that symbol or symbol+side.
2. Compare against the fixed `raw_edge_min5` oracle as an upper bound.
3. Keep the full-universe TP3 run as the negative control.
4. Only compare DCA after the no-DCA walk-forward selector remains positive with at least 50 opened trades.
