# Edge68 Selector Audit - 2026-05-16

Mode: research-only, paper backtests only. Do not touch live/daemon/order execution.

## What Was Reproduced

The external handoff result is reproducible locally on the 720h 1m NPZ:

- Signals: `obw_platform/meta_strategies/telegram_dca_mvp/reports/telegram_signals_raw_edge_min5.csv`
- Rows: 83 selected, 68 opened, 15 rejected
- Execution: `close_in_zone`, hard TTL 3600s, TP3, `edge_in_zone` weights, move stop after TP, stop-first
- PnL: `+9.2276%`
- MDD: `-1.1122%`
- PnL/MDD: `8.2967`

Same execution on all extracted Telegram signals is negative:

- Signals: `DB/telegram_signal_standard_bt/telegram_signals_extracted.csv`
- Rows: 312 total, 251 opened, 54 rejected, 7 missing
- PnL: `-16.1257%`
- MDD: `-20.1267%`
- PnL/MDD: `-0.8012`

## Cohort Difference

The selected subset does not materially differ from the rest by simple signal geometry:

- Leverage median: selected `30`, non-selected `30`
- Entry zone median: selected `8.74%`, non-selected `8.86%`
- SL extra median: selected `3.97%`, non-selected `3.93%`
- TP1 distance median: selected `1.45%`, non-selected `1.41%`
- TP1/SL RR median: selected `0.367`, non-selected `0.380`

The main visible difference is cohort composition:

- Selected symbols: `DOT`, `AAVE`, `NEAR`, `ADA`, `ORDI`, `JUP`, `APT`, `OP`, `ENS`
- Non-selected heavy losers include: `RENDER`, `SUI`, `INJ`, `XRP`, `LINK`, `PYTH`, `GALA`, `FET`, `LDO`, `JTO`, `ONDO`, `ROSE`, `ICP`
- Selected signal hours are tightly clustered around `10:00-15:00 UTC`, mostly `11:00-12:00 UTC`.

## Interpretation

`edge_min5` currently behaves like a symbol/cohort selector, not like a universal Telegram execution rule. It may still be useful, but it must be treated as potentially in-sample until the selector source is reconstructed.

## Next Loop Task

Find a causal selector that approximates `edge_min5` using only information available at or before entry:

1. Reconstruct how `telegram_signals_raw_edge_min5.csv` was generated.
2. Mark every selector feature as signal-time-known or future-leaky.
3. Test a symbol/cohort selector with walk-forward splits, not full-sample symbol PnL.
4. Keep TP3 + move-stop as the positive selected-subset baseline and all-signal TP3 as the negative control.
5. Keep no-DCA first; compare DCA only after a defensible no-DCA selector exists.
