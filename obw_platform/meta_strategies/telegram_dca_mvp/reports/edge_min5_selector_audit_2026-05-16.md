# Edge Min5 Selector Audit 2026-05-16

Mode: research-only, paper backtests only.

## Finding

`telegram_signals_raw_edge_min5.csv` is effectively a symbol-level subset, not a broad signal-time feature selector.

Selected symbols and row counts:

- DOT: 14
- AAVE: 11
- NEAR: 10
- ADA: 10
- ORDI: 9
- JUP: 9
- APT: 8
- OP: 7
- ENS: 5

The non-selected set has no rows for these symbols. That means the current positive subset is best understood as "historically profitable symbols" until proven otherwise.

## Geometry Check

Basic signal-time geometry is similar between selected and non-selected sets:

- Selected median `zone_pct`: `0.08664`; non-selected median: `0.08851`.
- Selected median `sl_extra_pct`: `0.04077`; non-selected median: `0.04057`.
- Selected median `tp1_pct`: `0.01395`; non-selected median: `0.01350`.
- Selected median `tp2_pct`: `0.03872`; non-selected median: `0.04028`.
- Selected median `tp3_pct`: `0.07831`; non-selected median after filtering invalid extreme values: `0.07813`.

No obvious entry geometry difference explains the edge.

## Reproduced Performance

Simple TP3 + `edge_in_zone` + move-stop after TP:

- Selected `edge_min5`: 68 opened trades, PnL `+9.2276%`, MDD `-1.1122%`, PnL/MDD `8.2967`.
- Full universe same params: 251 opened trades, PnL `-16.1257%`, MDD `-20.1267%`, PnL/MDD `-0.8012`.

## Time Split Diagnostic

For the 68 opened selected-subset trades:

- First 34 trades: +60.6244 USDT PnL, from 2025-01-01 to 2025-10-01.
- Last 34 trades: +31.6517 USDT PnL, from 2025-10-03 to 2026-05-04.

The edge persists in the second half but is weaker. This is only a diagnostic split, not a valid selector proof.

## Implication For Loop

The next research loop should not tune DCA on `edge_min5` as if it were out-of-sample. The correct next task is:

1. Reconstruct how the symbol list was selected.
2. Build a walk-forward symbol selector using only prior trades or prior TP-hit/MFE stats.
3. Compare against the fixed selected-symbol list as an in-sample upper bound.
4. Only after a causal selector survives, compare DCA against simple TP3 on that selected universe.
