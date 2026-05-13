# Akela Margin-Zero Codex Report

Updated: 2026-05-13T01:16:24Z

## Objective

Find V21-style experimental parameter configurations for the first Akela basket candidates that remove margin-call events without changing exchange, fee, slippage, liquidation, margin, or backtest math.

## Best Current Candidates

All generated candidates below are experimental YAMLs under `obw_platform/meta_strategies/akela_meta_short/generated_configs/margin_zero/`. `FREEDOMMONEY/USDT:USDT` keeps the existing production candidate because it was already zero-margin in the baseline basket.

| symbol | config | return_mtm_% | mdd_mtm_% | trades | margin_calls | bars_in_margin_call | terminal_unrealized_to_realized_ratio | raw log |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `IDOL/USDT:USDT` | `V21_idol_margin_zero_budget125.yaml` | 34.70 | -13.40 | 5848 | 0 | 0 | -0.2055 | `_reports/akela_meta_short/margin_zero_codex_loop/idol_budget125_full.log` |
| `FREEDOMMONEY/USDT:USDT` | `V21_freedommoney_bingx_live_candidate_1m_1y.yaml` | 64.28 | -24.09 | 7583 | 0 | 0 | -0.4130 | `_reports/akela_meta_short/margin_zero_codex_loop/freedommoney_baseline_full.log` |
| `MAXXING/USDT:USDT` | `V21_maxxing_margin_zero_budget125.yaml` | 104.68 | -21.17 | 4469 | 0 | 0 | -0.2273 | `_reports/akela_meta_short/margin_zero_codex_loop/maxxing_budget125_full.log` |
| `SUP/USDT:USDT` | `V21_sup_margin_zero_budget32_fast_exit.yaml` | 3.27 | -25.09 | 911 | 0 | 0 | -0.6354 | `_reports/akela_meta_short/margin_zero_codex_loop/sup_budget32_fast_exit_full.log` |

## Four-Symbol Basket Validation

| metric | value |
| --- | ---: |
| symbols | 4 |
| equal-weight terminal return approximation | 51.73% |
| worst single-symbol MTM drawdown | -25.09% |
| total trades | 18811 |
| total margin-call events | 0 |
| total bars in margin call | 0 |

## Current Cycle

The first-basket risk-cleanup target is still satisfied: all four selected configs have `margin_call_events_total = 0` and `bars_in_margin_call = 0` on full-year runs.

Selection note: `V21_sup_margin_zero_budget30_fast_exit.yaml` improves SUP full-year return from 1.90% to 2.87%, slightly improves MDD from -25.15% to -25.05%, and improves terminal unrealized/realized ratio from -0.7934 to -0.6742 versus plain budget30. Budget20 remains a valid lower-drawdown fallback, but fast-exit budget30 is the better current risk-cleanup balance.

Gap-test note: `V21_sup_margin_zero_budget45.yaml` had the best return in the new gap test at 5.61% with zero margin calls, but its full-year MDD was -45.45%. Budgets 35 and 40 also stayed zero-margin, but drew down -48.87% and -54.22%. This confirms that simply raising the SUP sizing budget recovers return while giving up too much drawdown for the immediate risk-cleanup target.

Near-budget note: `V21_sup_margin_zero_budget32.yaml` improved SUP full-year return to 2.65% with zero margin calls, but MDD was -30.41%. That makes it a useful secondary if a slightly deeper drawdown is acceptable, but it does not replace budget30 as the primary risk-cleanup pick. `V21_sup_margin_zero_budget31.yaml` also stayed zero-margin, but had slightly worse MDD (-30.46%) and lower return (2.35%) than budget32.

Tuner note: a 180-second 20k-bar tuner pass from `V21_sup_margin_zero_budget30.yaml` improved the slice to 4.91% return, -19.02% MDD, and zero margin calls. Its full-year confirmation stayed zero-margin, but returned 1.86% with -25.38% MDD, slightly worse than budget30's 1.90% and -25.15%. It is rejected as a primary candidate and was not promoted into `generated_configs`.

MAXXING best remains `V21_maxxing_margin_zero_budget125.yaml`; the previous short-cap sweep did not improve its full-year risk/return balance.

New SUP ladder note: the rejected higher-return SUP ladder variants had already shown that more active short-side ladders can lift return, but their full-year MDD rose near -50% when paired with larger sizing budgets. This cycle isolated that idea inside the budget30 sizing envelope. Both new 20k-bar tests stayed zero-margin, but neither earned full-year promotion: the combined long/short ladder variant only improved the slice by 0.06 return points while worsening MDD, and the short-only ladder variant underperformed budget30.

Fast-exit confirmation note: `V21_sup_margin_zero_budget30_fast_exit.yaml` looked only marginally better than plain budget30 on the 20k slice, but the full-year run confirmed a useful primary upgrade with no margin calls, slightly lower MDD, and materially better terminal unrealized exposure.

Latest follow-up note: transplanting the SUP fast-exit profile into IDOL/MAXXING budget125 stayed zero-margin on 20k slices, but worsened risk-adjusted performance. IDOL budget150 was reconstructed from the prior temporary screen and full-year confirmed; it also stayed zero-margin, but underperformed IDOL budget125 on return, drawdown, and terminal unrealized exposure. The selected basket is unchanged.

MAXXING tuner follow-up: a 240-second 20k-bar tuner pass from `V21_maxxing_margin_zero_budget125.yaml` found a zero-margin slice improvement by widening long full TP, tightening long partial exit, tightening long spacing, and making short exits faster. Full-year confirmation stayed zero-margin, but underperformed the current MAXXING pick on return, drawdown, and terminal unrealized ratio, so the tuned YAML was not promoted.

SUP cap follow-up: a fresh 20k-bar check tested mild MAXXING-derived SUP exposure caps and deeper low-exposure/min-budget probes. All variants still failed the margin-zero target with 26-29 margin-call events and were not promoted.

SUP budget32 fast-exit follow-up: `V21_sup_margin_zero_budget32_fast_exit.yaml` changes only the long/short `equityForSizingUSDT` budget from 30 to 32 versus the prior selected fast-exit config. It improved the 20k slice and then full-year confirmed with zero margin calls. Full-year return improved from 2.87% to 3.27% and terminal unrealized/realized ratio improved from -0.6742 to -0.6354. MDD was effectively unchanged but slightly worse at -25.09% versus -25.05%, so this is a narrow return/tail upgrade inside the same risk envelope.

Latest SUP micro-sweep note: a budget33 fast-exit probe and a budget32 tighter-long-exit probe both stayed zero-margin on 20k slices, but neither improved the selected `V21_sup_margin_zero_budget32_fast_exit.yaml`. Budget33 worsened return, MDD, and tail ratio versus budget32. Tightening the long exit from 0.20/0.36 to 0.18/0.34 marginally improved 20k MDD by 0.02 percentage points, but reduced return and worsened terminal unrealized ratio. Both temporary generated YAMLs were removed; raw logs were retained.

Latest MAXXING short-budget note: a short-side-only budget reduction from 125 to 110 stayed zero-margin on the 20k slice and slightly improved the terminal unrealized/realized ratio from -0.3540 to -0.3530, but it reduced return from 23.10% to 22.93% and worsened MDD from -8.31% to -8.36%. The temporary YAML was removed; the selected MAXXING budget125 config remains unchanged.

## Current Cycle Attempts

| config | limit | return_mtm_% | mdd_mtm_% | trades | margin_calls | bars_in_margin_call | tail ratio | result | raw log |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `V21_idol_margin_zero_budget125_fast_exit.yaml` | 20000 | -0.71 | -2.10 | 260 | 0 | 0 | -3.5281 | Rejected before full-year: stayed zero-margin, but worsened return, MDD, and terminal unrealized ratio versus IDOL budget125. | `_reports/akela_meta_short/margin_zero_codex_loop/idol_budget125_fast_exit_20k.log` |
| `V21_maxxing_margin_zero_budget125_fast_exit.yaml` | 20000 | 9.06 | -15.03 | 1498 | 0 | 0 | -0.7675 | Rejected before full-year: stayed zero-margin, but cut return from 23.10% to 9.06% and worsened MDD from -8.31% to -15.03% versus MAXXING budget125. | `_reports/akela_meta_short/margin_zero_codex_loop/maxxing_budget125_fast_exit_20k.log` |
| `V21_idol_margin_zero_budget150.yaml` | 20000 | -0.28 | -1.85 | 350 | 0 | 0 | -1.4251 | Passed 20k recheck and matched the prior temporary screen exactly; promoted only to full-year confirmation. | `_reports/akela_meta_short/margin_zero_codex_loop/idol_budget150_recheck_20k.log` |
| `V21_idol_margin_zero_budget150.yaml` | full | 29.43 | -15.81 | 6583 | 0 | 0 | -0.3544 | Rejected after full-year: zero-margin, but worse than IDOL budget125's 34.70% return, -13.40% MDD, and -0.2055 tail ratio. | `_reports/akela_meta_short/margin_zero_codex_loop/idol_budget150_full.log` |
| `V21_maxxing_margin_zero_budget125_tuned_exit.yaml` | 20000 | 32.94 | -7.73 | 1316 | 0 | 0 | -0.2794 | Tuner slice candidate from budget125; promoted only to full-year confirmation because it improved 20k return and MDD while staying zero-margin. | `_reports/akela_meta_short/margin_zero_codex_loop/maxxing_budget125_tuner_20k_20260513.log` |
| `V21_maxxing_margin_zero_budget125_tuned_exit.yaml` | full | 97.71 | -22.12 | 4211 | 0 | 0 | -0.2401 | Rejected after full-year: zero-margin, but worse than MAXXING budget125's 104.68% return, -21.17% MDD, and -0.2273 tail ratio. Temporary generated YAML was removed. | `_reports/akela_meta_short/margin_zero_codex_loop/maxxing_budget125_tuned_exit_full.log` |
| `V21_sup_margin_zero_cap_l120_s110.yaml` | 20000 | -25.68 | -238.54 | 1052 | 29 | 551 | -1.6004 | Rejected before full-year: mild max-invest caps did not prevent SUP tail exposure or margin calls. | `_reports/akela_meta_short/margin_zero_codex_loop/backtests/SUP_cap_l120_s110_limit20000.log` |
| `V21_sup_margin_zero_cap_l100_s100.yaml` | 20000 | -25.68 | -238.54 | 1052 | 29 | 551 | -1.6004 | Rejected before full-year: same failed tail profile as cap_l120_s110. | `_reports/akela_meta_short/margin_zero_codex_loop/backtests/SUP_cap_l100_s100_limit20000.log` |
| `V21_sup_margin_zero_low_base_l090_s090.yaml` | 20000 | -26.02 | -238.17 | 1192 | 26 | 557 | -1.6130 | Rejected before full-year: lower base sizing plus wider spacing still failed the margin-zero target. | `_reports/akela_meta_short/margin_zero_codex_loop/backtests/SUP_low_base_l090_s090_limit20000.log` |
| temporary SUP low exposure 0.25/0.60 | 20000 | -26.02 | -238.17 | 1192 | 26 | 557 | -1.6130 | Rejected before full-year: lowering min/max exposure floors did not change the failed SUP tail profile enough; temporary YAML removed. | `_reports/akela_meta_short/margin_zero_codex_loop/backtests/SUP_low_exposure_025_060_limit20000.log` |
| temporary SUP micro 0.10/0.30 | 20000 | -24.78 | -207.23 | 1226 | 27 | 459 | -1.5988 | Rejected before full-year: drawdown improved but margin calls persisted; temporary YAML removed. | `_reports/akela_meta_short/margin_zero_codex_loop/backtests/SUP_micro_010_030_limit20000.log` |
| `V21_sup_margin_zero_budget32_fast_exit.yaml` | 20000 | 4.80 | -17.46 | 228 | 0 | 0 | -0.6203 | Promoted to full-year confirmation: improved 20k return, MDD, and terminal unrealized ratio versus SUP budget30_fast_exit while staying zero-margin. | `_reports/akela_meta_short/margin_zero_codex_loop/sup_budget32_fast_exit_20k.log` |
| `V21_sup_margin_zero_budget32_fast_exit.yaml` | full | 3.27 | -25.09 | 911 | 0 | 0 | -0.6354 | Promoted as current SUP pick: full-year zero-margin with better return and tail ratio than budget30_fast_exit; MDD was slightly worse by 0.04 percentage points. | `_reports/akela_meta_short/margin_zero_codex_loop/sup_budget32_fast_exit_full.log` |
| temporary `V21_sup_margin_zero_budget33_fast_exit.yaml` | 20000 | 4.72 | -17.64 | 232 | 0 | 0 | -0.6252 | Rejected before full-year: stayed zero-margin, but worsened return, MDD, and tail ratio versus selected budget32_fast_exit. Temporary YAML removed. | `_reports/akela_meta_short/margin_zero_codex_loop/sup_budget33_fast_exit_20k.log` |
| temporary `V21_sup_margin_zero_budget32_long_exit018.yaml` | 20000 | 4.69 | -17.43 | 231 | 0 | 0 | -0.6257 | Rejected before full-year: tighter long exits gave only a tiny MDD improvement while reducing return and worsening tail ratio versus selected budget32_fast_exit. Temporary YAML removed. | `_reports/akela_meta_short/margin_zero_codex_loop/sup_budget32_long_exit018_20k.log` |
| temporary `V21_maxxing_margin_zero_short_budget110.yaml` | 20000 | 22.93 | -8.36 | 1318 | 0 | 0 | -0.3530 | Rejected before full-year: short-only budget reduction barely improved tail ratio, while reducing return and worsening MDD versus selected MAXXING budget125. Temporary YAML removed. | `_reports/akela_meta_short/margin_zero_codex_loop/maxxing_short_budget110_20k.log` |

## Baseline Comparison

| symbol | baseline return_mtm_% | baseline mdd_mtm_% | baseline margin_calls | margin-zero return_mtm_% | margin-zero mdd_mtm_% | margin-zero margin_calls |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `IDOL/USDT:USDT` | 44.19 | -37.75 | 18 | 34.70 | -13.40 | 0 |
| `FREEDOMMONEY/USDT:USDT` | 64.28 | -24.09 | 0 | 64.28 | -24.09 | 0 |
| `MAXXING/USDT:USDT` | 183.80 | -18.37 | 8 | 104.68 | -21.17 | 0 |
| `SUP/USDT:USDT` | -1.05 | -219.88 | 35 | 3.27 | -25.09 | 0 |

## Exact Full-Year Commands

```bash
python3 obw_platform/backtester_dual_long_short_fast_pack_v2.py --cfg obw_platform/meta_strategies/akela_meta_short/generated_configs/margin_zero/V21_idol_margin_zero_budget125.yaml --npz DB/akela_meta_short_1m_1y_idol_bingx.npz --symbol IDOL/USDT:USDT
```
```bash
python3 obw_platform/backtester_dual_long_short_fast_pack_v2.py --cfg obw_platform/configs/V21_freedommoney_bingx_live_candidate_1m_1y.yaml --npz DB/fast_cache_1m_freedommoney_1y_bingx.npz --symbol FREEDOMMONEY/USDT:USDT
```
```bash
python3 obw_platform/backtester_dual_long_short_fast_pack_v2.py --cfg obw_platform/meta_strategies/akela_meta_short/generated_configs/margin_zero/V21_maxxing_margin_zero_budget125.yaml --npz DB/fast_cache_1m_maxxing_1y_bingx.npz --symbol MAXXING/USDT:USDT
```
```bash
python3 obw_platform/backtester_dual_long_short_fast_pack_v2.py --cfg obw_platform/meta_strategies/akela_meta_short/generated_configs/margin_zero/V21_sup_margin_zero_budget32_fast_exit.yaml --npz DB/akela_meta_short_1m_1y_sup_bingx.npz --symbol SUP/USDT:USDT
```

## Next Action

First-basket margin-call cleanup remains satisfied with 0 total margin-call events using SUP budget32_fast_exit. The narrow SUP 31-33/exit-threshold follow-up and the MAXXING short-budget110 probe did not improve the current picks. The next useful search is a MAXXING exit-shape hypothesis rather than another simple short-budget reduction, because lower short budget reduced activity/return while leaving the 20k tail profile almost unchanged.
