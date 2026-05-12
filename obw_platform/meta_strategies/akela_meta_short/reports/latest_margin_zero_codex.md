# Akela Margin-Zero Codex Report

Updated: 2026-05-12T22:41:30Z

## Objective

Find V21-style experimental parameter configurations for the first Akela basket candidates that remove margin-call events without changing exchange, fee, slippage, liquidation, margin, or backtest math.

## Best Current Candidates

All candidates below are experimental YAMLs under `obw_platform/meta_strategies/akela_meta_short/generated_configs/margin_zero/`.

| symbol | config | return_mtm_% | mdd_mtm_% | trades | margin_calls | bars_in_margin_call | terminal_unrealized_to_realized_ratio | raw log |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `IDOL/USDT:USDT` | `V21_idol_margin_zero_budget50.yaml` | 9.05 | -10.40 | 2430 | 0 | 0 | -0.4943 | `_reports/akela_meta_short/margin_zero_codex_loop/idol_budget50_full.log` |
| `MAXXING/USDT:USDT` | `V21_maxxing_margin_zero_budget125.yaml` | 104.68 | -21.17 | 4469 | 0 | 0 | -0.2273 | `_reports/akela_meta_short/margin_zero_codex_loop/maxxing_budget125_full.log` |
| `SUP/USDT:USDT` | `V21_sup_margin_zero_short_ladder_soft.yaml` | 9.04 | -48.02 | 1329 | 0 | 0 | -0.3140 | `_reports/akela_meta_short/margin_zero_codex_loop/sup_short_ladder_soft_full.log` |

`FREEDOMMONEY/USDT:USDT` already had zero margin calls in the latest baseline basket with `V21_freedommoney_bingx_live_candidate_1m_1y.yaml`: return 64.28%, MDD -24.09%, trades 7583, margin calls 0.

## Four-Symbol Basket Validation

Validated with the three generated margin-zero configs plus the existing FREEDOMMONEY baseline-zero config.

| metric | value |
| --- | ---: |
| symbols | 4 |
| equal-weight terminal return approximation | 46.76% |
| worst single-symbol MTM drawdown | -48.02% |
| total trades | 15811 |
| total margin-call events | 0 |
| total bars in margin call | 0 |

| symbol | config | return_mtm_% | mdd_mtm_% | trades | margin_calls | bars_in_margin_call | tail unrealized/realized | raw log |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `IDOL/USDT:USDT` | `V21_idol_margin_zero_budget50.yaml` | 9.05 | -10.40 | 2430 | 0 | 0 | -0.4943 | `_reports/akela_meta_short/margin_zero_codex_loop/idol_budget50_full.log` |
| `FREEDOMMONEY/USDT:USDT` | `V21_freedommoney_bingx_live_candidate_1m_1y.yaml` | 64.28 | -24.09 | 7583 | 0 | 0 | -0.4130 | `_reports/akela_meta_short/margin_zero_codex_loop/freedommoney_baseline_full.log` |
| `MAXXING/USDT:USDT` | `V21_maxxing_margin_zero_budget125.yaml` | 104.68 | -21.17 | 4469 | 0 | 0 | -0.2273 | `_reports/akela_meta_short/margin_zero_codex_loop/maxxing_budget125_full.log` |
| `SUP/USDT:USDT` | `V21_sup_margin_zero_short_ladder_soft.yaml` | 9.04 | -48.02 | 1329 | 0 | 0 | -0.3140 | `_reports/akela_meta_short/margin_zero_codex_loop/sup_short_ladder_soft_full.log` |

## Baseline Comparison

| symbol | baseline return_mtm_% | baseline mdd_mtm_% | baseline margin_calls | margin-zero return_mtm_% | margin-zero mdd_mtm_% | margin-zero margin_calls |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `IDOL/USDT:USDT` | 44.19 | -37.75 | 18 | 9.05 | -10.40 | 0 |
| `MAXXING/USDT:USDT` | 183.80 | -18.37 | 8 | 104.68 | -21.17 | 0 |
| `SUP/USDT:USDT` | -1.05 | -219.88 | 35 | 9.04 | -48.02 | 0 |

## What Changed

The useful first-cycle hypothesis was that `maxLongInvestPct` and `maxShortInvestPct` do not cap the full DCA cycle budget. The confirmed margin-zero family lowers `equityForSizingUSDT`, reduces fill density, and widens DCA spacing without touching portfolio, fee, slippage, liquidation, margin, exchange, or backtester math.

This cycle improved SUP by making the sizing asymmetric around the existing budget50 candidate:

- `V21_sup_margin_zero_long35_short50.yaml` keeps the short side at budget50.
- The long side uses `equityForSizingUSDT: 35`, `baseOrderPctEq: 0.35`, `maxLongInvestPct: 0.35`, and `minLongInvestPct: 0.15`.
- Full-year SUP stayed at `margin_call_events_total: 0`, improved return from 5.34% to 11.27%, and reduced terminal unrealized drag from -21.27 USDT to -8.52 USDT. Full-year MDD remained roughly unchanged near -50%.

Follow-up risk cleanup tested small changes around `V21_sup_margin_zero_long35_short50.yaml`. The useful variant is `V21_sup_margin_zero_short_ladder_soft.yaml`, which keeps the same long35/short50 sizing and softens the short DCA multipliers from `1.5/1.0/1.2/1.5` to `1.2/1.0/1.1/1.2`. Full-year SUP stayed at zero margin calls and improved MDD from -50.20% to -48.02%, at the cost of lower MTM return, 9.04% vs 11.27%, and slightly worse terminal unrealized/realized ratio, -0.3140 vs -0.2744.

This cycle tested whether a halfway short ladder could recover some lost return while keeping the soft ladder's drawdown improvement. `V21_sup_margin_zero_short_ladder_mid.yaml` used short multipliers `1.35/1.0/1.15/1.35`. It passed the 20k margin-zero slice, but full-year MDD worsened to -53.81%, so the best SUP candidate remains `V21_sup_margin_zero_short_ladder_soft.yaml`.

This cycle tested small long-side risk cleanup around `V21_sup_margin_zero_short_ladder_soft.yaml`. Lowering the long budget to 30 USDT and softening only the long DCA ladder both stayed zero-margin on the 20k slice but worsened the return/drawdown/terminal-drag balance. Widening only long DCA spacing had the best 20k slice drawdown, so it was confirmed full-year, but it was effectively identical and slightly worse than the current SUP winner: 9.03% MTM, -48.02% MDD, 0 margin calls vs 9.04% MTM, -48.02% MDD, 0 margin calls. The three rejected temporary YAMLs were removed after logging the raw results.

This cycle ran the existing V21 tuner from `V21_sup_margin_zero_short_ladder_soft.yaml` on a 20k-bar SUP slice with the margin-call penalty intact. The tuner found a cleaner 20k slice, but its final YAML included negative `maxLongInvestPct` and `maxShortInvestPct` values after delta exposure stages; those cap changes did not improve score and were not promoted. A cleaned variant kept the positive exposure caps and confirmed the 20k result exactly, then failed full-year confirmation: 5.84% MTM, -57.89% MDD, 0 margin calls, and terminal unrealized/realized ratio -0.5170. The cleaned temporary YAML was removed, and the best SUP candidate remains `V21_sup_margin_zero_short_ladder_soft.yaml`.

This cycle ran a MAXXING budget-lift probe from `V21_maxxing_margin_zero_budget50.yaml`. Budget75, budget100, and budget125 all passed the 20k slice at zero margin calls. Budget150 and budget175 also avoided margin calls on the slice, but terminal unrealized drag worsened sharply, so they were rejected without full-year confirmation. Full-year confirmation showed budget125 was the useful upgrade: 104.68% MTM, -21.17% MDD, 4469 trades, 0 margin calls, 0 margin-call bars, and terminal unrealized/realized ratio -0.2273. Budget75 and budget100 were valid but inferior on full-year return and drawdown, so their temporary YAMLs were removed; `V21_maxxing_margin_zero_budget125.yaml` is the new MAXXING best candidate.

## Attempts

| config | symbol/scope | limit | result | reason |
| --- | --- | ---: | --- | --- |
| `baseline V21_freedommoney_bingx_live_candidate_1m_1y.yaml` | `SUP/USDT:USDT` | 20000 | 23 margin calls | Reproduced the risk problem on a short slice. |
| `baseline V21_maxxing_bingx_live_candidate_1m_1y.yaml` | `SUP/USDT:USDT` | 20000 | 29 margin calls | Not safer for SUP. |
| `V21_sup_margin_zero_cap075.yaml` | `SUP/USDT:USDT` | 20000 | 18 margin calls | Adaptive max-invest caps alone did not cap DCA cycle exposure. |
| `V21_sup_margin_zero_cap050.yaml` | `SUP/USDT:USDT` | 20000 | 15 margin calls | Improved but still failed margin-zero target. |
| `V21_sup_margin_zero_long035_short075.yaml` | `SUP/USDT:USDT` | 20000 | 22 margin calls | Terminal MTM improved, but drawdown still crossed margin threshold. |
| `V21_sup_margin_zero_risk_exit_budget50.yaml` | `SUP/USDT:USDT` | 20000 | 6 margin calls | Stale/contrary exits realized losses but did not fully prevent threshold crossings. |
| `V21_sup_margin_zero_budget25.yaml` | `SUP/USDT:USDT` | full | 0 margin calls, 0 bars in margin call, 1.31% MTM, MDD -29.42% | Valid but more conservative than budget50. |
| `V21_sup_margin_zero_budget55.yaml` | `SUP/USDT:USDT` | 20000 | 0 margin calls, 0 bars in margin call, 3.33% MTM, MDD -14.81% | Clean slice; full-year confirmation required because SUP behavior is path-sensitive. |
| `V21_sup_margin_zero_budget55.yaml` | `SUP/USDT:USDT` | full | 0 margin calls, 0 bars in margin call, 4.34% MTM, MDD -51.53% | Valid but inferior to budget50 on return and drawdown. |
| `V21_sup_margin_zero_budget60.yaml` | `SUP/USDT:USDT` | 20000 | 0 margin calls, 0 bars in margin call, 4.40% MTM, MDD -14.64% | Clean slice, but showed worse tail unrealized ratio than budget50. |
| `V21_sup_margin_zero_budget60.yaml` | `SUP/USDT:USDT` | full | 0 margin calls, 0 bars in margin call, 3.93% MTM, MDD -62.52% | Valid but inferior to budget50 on return and drawdown. |
| `V21_sup_margin_zero_budget65.yaml` | `SUP/USDT:USDT` | 20000 | 4 margin calls, 81 bars in margin call, -0.97% MTM, MDD -113.56% | Budget increase crossed the risk threshold on the 20k slice. |
| `V21_sup_margin_zero_budget75.yaml` | `SUP/USDT:USDT` | 20000 | 10 margin calls, 59 bars in margin call, -12.05% MTM, MDD -103.81% | Rejected immediately; higher sizing restored margin-call risk. |
| `V21_sup_margin_zero_long35_short50.yaml` | `SUP/USDT:USDT` | 20000 | 0 margin calls, 0 bars in margin call, 10.95% MTM, MDD -16.21% | Promising slice: zero margin calls, better return than budget50 20k, lower terminal unrealized ratio. |
| `V21_sup_margin_zero_long35_short50.yaml` | `SUP/USDT:USDT` | full | 0 margin calls, 0 bars in margin call, 11.27% MTM, MDD -50.20% | Confirmed full-year: zero margin calls, higher return than budget50 and much smaller terminal unrealized drag; MDD roughly unchanged. |
| `V21_sup_margin_zero_wider_spacing.yaml` | `SUP/USDT:USDT` | 20000 | 0 margin calls, 0 bars in margin call, 1.68% MTM, MDD -9.91% | Rejected for now: wider long/short DCA spacing reduced slice drawdown but gave up too much MTM and worsened terminal unrealized ratio to -0.8894. |
| `V21_sup_margin_zero_wider_soft.yaml` | `SUP/USDT:USDT` | 20000 | 0 margin calls, 0 bars in margin call, 1.65% MTM, MDD -9.20% | Rejected for now: lowest slice drawdown, but MTM return and terminal unrealized ratio were inferior. |
| `V21_sup_margin_zero_short_ladder_soft.yaml` | `SUP/USDT:USDT` | 20000 | 0 margin calls, 0 bars in margin call, 11.00% MTM, MDD -15.17% | Promising slice: zero-margin, similar return to long35_short50 20k, and slightly lower drawdown. |
| `V21_sup_margin_zero_short_ladder_soft.yaml` | `SUP/USDT:USDT` | full | 0 margin calls, 0 bars in margin call, 9.04% MTM, MDD -48.02% | Risk-first SUP replacement: lower return than long35_short50 but better full-year MDD while keeping margin calls at zero. |
| `V21_sup_margin_zero_short_ladder_mid.yaml` | `SUP/USDT:USDT` | 20000 | 0 margin calls, 0 bars in margin call, 11.04% MTM, MDD -15.66% | Clean slice, but already gave up most of the soft-ladder drawdown gain. |
| `V21_sup_margin_zero_short_ladder_mid.yaml` | `SUP/USDT:USDT` | full | 0 margin calls, 0 bars in margin call, 10.83% MTM, MDD -53.81% | Rejected: recovered some return vs short_ladder_soft but full-year drawdown was worse than both short_ladder_soft and long35_short50. |
| `V21_sup_margin_zero_short_exit_mild.yaml` | `SUP/USDT:USDT` | 20000 | 0 margin calls, 0 bars in margin call, 11.00% MTM, MDD -15.17% | Rejected and config removed: mild short TP/callback changes were behaviorally identical to short_ladder_soft on the 20k slice. |
| `V21_sup_margin_zero_long25_short50.yaml` | `SUP/USDT:USDT` | 20000 | 0 margin calls, 0 bars in margin call, -0.80% MTM, MDD -18.30% | Rejected: zero-margin but under-sized long side left negative MTM return on the 20k slice. |
| `V21_sup_margin_zero_long_exit_fast.yaml` | `SUP/USDT:USDT` | 20000 | 0 margin calls, 0 bars in margin call, 1.55% MTM, MDD -15.88% | Rejected: faster long exits stayed zero-margin but return and terminal unrealized ratio were inferior to long35_short50. |
| `V21_sup_margin_zero_long30_short50.yaml` | `SUP/USDT:USDT` | 20000 | 0 margin calls, 0 bars in margin call, 2.86% MTM, MDD -15.72% | Rejected and config removed: lower long budget stayed zero-margin but gave up too much return and worsened terminal unrealized/realized ratio to -0.7550. |
| `V21_sup_margin_zero_long_ladder_soft.yaml` | `SUP/USDT:USDT` | 20000 | 0 margin calls, 0 bars in margin call, 9.40% MTM, MDD -15.39% | Rejected and config removed: softening only the long DCA ladder was inferior to short_ladder_soft on return, drawdown, and terminal drag. |
| `V21_sup_margin_zero_long_spacing_soft.yaml` | `SUP/USDT:USDT` | 20000 | 0 margin calls, 0 bars in margin call, 1.74% MTM, MDD -13.83% | Best 20k drawdown among this long-side cleanup batch, so it received full-year confirmation. |
| `V21_sup_margin_zero_long_spacing_soft.yaml` | `SUP/USDT:USDT` | full | 0 margin calls, 0 bars in margin call, 9.03% MTM, MDD -48.02% | Rejected and config removed: full-year behavior was effectively identical and slightly worse than short_ladder_soft. |
| `V21_maxxing_margin_zero_budget50.yaml` | `MAXXING/USDT:USDT` | full | 0 margin calls, 0 bars in margin call, 26.23% MTM, MDD -11.60% | Confirmed margin-zero candidate. |
| `V21_maxxing_margin_zero_budget75.yaml` | `MAXXING/USDT:USDT` | 20000 | 0 margin calls, 0 bars in margin call, 22.71% MTM, MDD -6.98% | Clean slice and better return than budget50 20k; promoted to full-year as safer intermediate. |
| `V21_maxxing_margin_zero_budget100.yaml` | `MAXXING/USDT:USDT` | 20000 | 0 margin calls, 0 bars in margin call, 20.79% MTM, MDD -7.88% | Clean slice, but lower return and worse drawdown than budget125 20k. |
| `V21_maxxing_margin_zero_budget125.yaml` | `MAXXING/USDT:USDT` | 20000 | 0 margin calls, 0 bars in margin call, 23.10% MTM, MDD -8.31% | Best budget-lift 20k slice before terminal drag worsened at 150/175; promoted to full-year. |
| `V21_maxxing_margin_zero_budget150.yaml` | `MAXXING/USDT:USDT` | 20000 | 0 margin calls, 0 bars in margin call, 3.88% MTM, MDD -23.16% | Rejected and config removed: terminal unrealized drag and drawdown worsened sharply on the slice. |
| `V21_maxxing_margin_zero_budget175.yaml` | `MAXXING/USDT:USDT` | 20000 | 0 margin calls, 0 bars in margin call, 10.30% MTM, MDD -17.50% | Rejected and config removed: terminal unrealized drag stayed materially worse than budget125. |
| `V21_maxxing_margin_zero_budget75.yaml` | `MAXXING/USDT:USDT` | full | 0 margin calls, 0 bars in margin call, 47.31% MTM, MDD -24.17% | Valid but inferior to budget125 on full-year return and drawdown; config removed. |
| `V21_maxxing_margin_zero_budget100.yaml` | `MAXXING/USDT:USDT` | full | 0 margin calls, 0 bars in margin call, 84.26% MTM, MDD -22.30% | Valid but inferior to budget125 on full-year return and drawdown; config removed. |
| `V21_maxxing_margin_zero_budget125.yaml` | `MAXXING/USDT:USDT` | full | 0 margin calls, 0 bars in margin call, 104.68% MTM, MDD -21.17% | New best MAXXING margin-zero candidate: large return recovery at zero margin calls. |
| `V21_idol_margin_zero_budget50.yaml` | `IDOL/USDT:USDT` | full | 0 margin calls, 0 bars in margin call, 9.05% MTM, MDD -10.40% | Confirmed margin-zero candidate. |
| `tuner final_best.yaml from akela_margin_zero_sup_soft_probe_20260512_220933` | `SUP/USDT:USDT` | 20000 | 0 margin calls, 0 bars in margin call, 12.38% MTM, MDD -8.90% | Bounded tuner probe improved the 20k slice, but final YAML included negative exposure caps after delta stages; those cap changes did not improve score and were not promoted. Raw log: `_reports/akela_meta_short/margin_zero_codex_loop/sup_soft_tuner_20k_20260512T220932Z.log`. |
| `V21_sup_margin_zero_tuner_spacing_exit.yaml` | `SUP/USDT:USDT` | 20000 | 0 margin calls, 0 bars in margin call, 12.38% MTM, MDD -8.90% | Cleaned tuner-derived variant kept positive exposure caps and reproduced the 20k tuner result exactly, so it received full-year confirmation. Raw log: `_reports/akela_meta_short/margin_zero_codex_loop/sup_tuner_spacing_exit_20k.log`. |
| `V21_sup_margin_zero_tuner_spacing_exit.yaml` | `SUP/USDT:USDT` | full | 0 margin calls, 0 bars in margin call, 5.84% MTM, MDD -57.89% | Rejected and config removed: full-year worsened return, MDD, and terminal drag versus `V21_sup_margin_zero_short_ladder_soft.yaml`. Raw log: `_reports/akela_meta_short/margin_zero_codex_loop/sup_tuner_spacing_exit_full.log`. |

## Exact Full-Year Commands

```bash
python3 obw_platform/backtester_dual_long_short_fast_pack_v2.py --cfg obw_platform/meta_strategies/akela_meta_short/generated_configs/margin_zero/V21_idol_margin_zero_budget50.yaml --npz DB/akela_meta_short_1m_1y_idol_bingx.npz --symbol IDOL/USDT:USDT
```
```bash
python3 obw_platform/backtester_dual_long_short_fast_pack_v2.py --cfg obw_platform/configs/V21_freedommoney_bingx_live_candidate_1m_1y.yaml --npz DB/fast_cache_1m_freedommoney_1y_bingx.npz --symbol FREEDOMMONEY/USDT:USDT
```
```bash
python3 obw_platform/backtester_dual_long_short_fast_pack_v2.py --cfg obw_platform/meta_strategies/akela_meta_short/generated_configs/margin_zero/V21_maxxing_margin_zero_budget125.yaml --npz DB/fast_cache_1m_maxxing_1y_bingx.npz --symbol MAXXING/USDT:USDT
```
```bash
python3 obw_platform/backtester_dual_long_short_fast_pack_v2.py --cfg obw_platform/meta_strategies/akela_meta_short/generated_configs/margin_zero/V21_sup_margin_zero_short_ladder_soft.yaml --npz DB/akela_meta_short_1m_1y_sup_bingx.npz --symbol SUP/USDT:USDT
```

## Next Action

The first-basket margin-call cleanup remains satisfied. MAXXING now has a stronger zero-margin candidate, `V21_maxxing_margin_zero_budget125.yaml`, improving from 26.23% to 104.68% MTM at 0 margin calls. The next useful work is IDOL second-pass cleanup for better return at zero margin calls, or a SUP tuner plan constrained to non-negative exposure caps.
