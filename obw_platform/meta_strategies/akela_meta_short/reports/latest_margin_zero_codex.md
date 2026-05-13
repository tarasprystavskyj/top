# Akela Margin-Zero Codex Report

Updated: 2026-05-13T02:03:33Z

## Objective

Find V21-style experimental parameter configurations for the first Akela basket candidates that remove margin-call events without changing exchange, fee, slippage, liquidation, margin, or backtest math.

## Best Current Candidates

All generated candidates below are experimental YAMLs under `obw_platform/meta_strategies/akela_meta_short/generated_configs/margin_zero/`. `FREEDOMMONEY/USDT:USDT` keeps the existing production candidate because it was already zero-margin in the baseline basket.

| symbol | config | return_mtm_% | mdd_mtm_% | trades | margin_calls | bars_in_margin_call | terminal_unrealized_to_realized_ratio | raw log |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `IDOL/USDT:USDT` | `V21_idol_margin_zero_budget125.yaml` | 34.70 | -13.40 | 5848 | 0 | 0 | -0.2055 | `_reports/akela_meta_short/margin_zero_codex_loop/idol_budget125_full.log` |
| `FREEDOMMONEY/USDT:USDT` | `V21_freedommoney_bingx_live_candidate_1m_1y.yaml` | 64.28 | -24.09 | 7583 | 0 | 0 | -0.4130 | `_reports/akela_meta_short/margin_zero_codex_loop/freedommoney_baseline_full.log` |
| `MAXXING/USDT:USDT` | `V21_maxxing_margin_zero_budget125_stress_exit.yaml` | 119.55 | -13.68 | 4996 | 0 | 0 | -0.2048 | `_reports/akela_meta_short/margin_zero_codex_loop/maxxing_budget125_stress_exit_full.log` |
| `SUP/USDT:USDT` | `V21_sup_margin_zero_budget32_fast_exit.yaml` | 3.27 | -25.09 | 911 | 0 | 0 | -0.6354 | `_reports/akela_meta_short/margin_zero_codex_loop/sup_budget32_fast_exit_full.log` |

## Four-Symbol Basket Validation

| metric | value |
| --- | ---: |
| symbols | 4 |
| equal-weight terminal return approximation | 55.45% |
| worst single-symbol MTM drawdown | -25.09% |
| total trades | 19338 |
| total margin-call events | 0 |
| total bars in margin call | 0 |

## Current Cycle

The first-basket risk-cleanup target is still satisfied: all four selected configs have `margin_call_events_total = 0` and `bars_in_margin_call = 0` on full-year runs.

Selection note: `V21_sup_margin_zero_budget30_fast_exit.yaml` improves SUP full-year return from 1.90% to 2.87%, slightly improves MDD from -25.15% to -25.05%, and improves terminal unrealized/realized ratio from -0.7934 to -0.6742 versus plain budget30. Budget20 remains a valid lower-drawdown fallback, but fast-exit budget30 is the better current risk-cleanup balance.

Gap-test note: `V21_sup_margin_zero_budget45.yaml` had the best return in the new gap test at 5.61% with zero margin calls, but its full-year MDD was -45.45%. Budgets 35 and 40 also stayed zero-margin, but drew down -48.87% and -54.22%. This confirms that simply raising the SUP sizing budget recovers return while giving up too much drawdown for the immediate risk-cleanup target.

Near-budget note: `V21_sup_margin_zero_budget32.yaml` improved SUP full-year return to 2.65% with zero margin calls, but MDD was -30.41%. That makes it a useful secondary if a slightly deeper drawdown is acceptable, but it does not replace budget30 as the primary risk-cleanup pick. `V21_sup_margin_zero_budget31.yaml` also stayed zero-margin, but had slightly worse MDD (-30.46%) and lower return (2.35%) than budget32.

Tuner note: a 180-second 20k-bar tuner pass from `V21_sup_margin_zero_budget30.yaml` improved the slice to 4.91% return, -19.02% MDD, and zero margin calls. Its full-year confirmation stayed zero-margin, but returned 1.86% with -25.38% MDD, slightly worse than budget30's 1.90% and -25.15%. It is rejected as a primary candidate and was not promoted into `generated_configs`.

MAXXING best is now `V21_maxxing_margin_zero_budget125_stress_exit.yaml`. A 25k-bar diagnosis showed the prior 20k screens missed the March 12 short-squeeze stress window: selected budget125 stayed zero-margin there, but had -51.97% MTM drawdown and a -1.5508 terminal unrealized/realized ratio on that slice. A bounded 25k-bar tuner pass tightened long and short exits without changing sizing, slippage, margin, exchange, fee, or liquidation settings. Full-year confirmation improved MAXXING from 104.68% return, -21.17% MDD, and -0.2273 tail ratio to 119.55% return, -13.68% MDD, and -0.2048 tail ratio, with zero margin calls.

New SUP ladder note: the rejected higher-return SUP ladder variants had already shown that more active short-side ladders can lift return, but their full-year MDD rose near -50% when paired with larger sizing budgets. This cycle isolated that idea inside the budget30 sizing envelope. Both new 20k-bar tests stayed zero-margin, but neither earned full-year promotion: the combined long/short ladder variant only improved the slice by 0.06 return points while worsening MDD, and the short-only ladder variant underperformed budget30.

Fast-exit confirmation note: `V21_sup_margin_zero_budget30_fast_exit.yaml` looked only marginally better than plain budget30 on the 20k slice, but the full-year run confirmed a useful primary upgrade with no margin calls, slightly lower MDD, and materially better terminal unrealized exposure.

Latest follow-up note: transplanting the SUP fast-exit profile into IDOL/MAXXING budget125 stayed zero-margin on 20k slices, but worsened risk-adjusted performance. IDOL budget150 was reconstructed from the prior temporary screen and full-year confirmed; it also stayed zero-margin, but underperformed IDOL budget125 on return, drawdown, and terminal unrealized exposure. The selected basket is unchanged.

MAXXING tuner follow-up: a 240-second 20k-bar tuner pass from `V21_maxxing_margin_zero_budget125.yaml` found a zero-margin slice improvement by widening long full TP, tightening long partial exit, tightening long spacing, and making short exits faster. Full-year confirmation stayed zero-margin, but underperformed the current MAXXING pick on return, drawdown, and terminal unrealized ratio, so the tuned YAML was not promoted.

SUP cap follow-up: a fresh 20k-bar check tested mild MAXXING-derived SUP exposure caps and deeper low-exposure/min-budget probes. All variants still failed the margin-zero target with 26-29 margin-call events and were not promoted.

SUP budget32 fast-exit follow-up: `V21_sup_margin_zero_budget32_fast_exit.yaml` changes only the long/short `equityForSizingUSDT` budget from 30 to 32 versus the prior selected fast-exit config. It improved the 20k slice and then full-year confirmed with zero margin calls. Full-year return improved from 2.87% to 3.27% and terminal unrealized/realized ratio improved from -0.6742 to -0.6354. MDD was effectively unchanged but slightly worse at -25.09% versus -25.05%, so this is a narrow return/tail upgrade inside the same risk envelope.

Latest SUP micro-sweep note: a budget33 fast-exit probe and a budget32 tighter-long-exit probe both stayed zero-margin on 20k slices, but neither improved the selected `V21_sup_margin_zero_budget32_fast_exit.yaml`. Budget33 worsened return, MDD, and tail ratio versus budget32. Tightening the long exit from 0.20/0.36 to 0.18/0.34 marginally improved 20k MDD by 0.02 percentage points, but reduced return and worsened terminal unrealized ratio. Both temporary generated YAMLs were removed; raw logs were retained.

Latest MAXXING short-budget note: a short-side-only budget reduction from 125 to 110 stayed zero-margin on the 20k slice and slightly improved the terminal unrealized/realized ratio from -0.3540 to -0.3530, but it reduced return from 23.10% to 22.93% and worsened MDD from -8.31% to -8.36%. The temporary YAML was removed; the selected MAXXING budget125 config remains unchanged.

MAXXING exit-shape follow-up: isolating the prior tuner exit improvements while restoring the selected budget125 spacing produced a strong 20k slice (31.19% return, -7.83% MDD, zero margin calls), but full-year confirmation rejected it at 94.20% return, -22.09% MDD, and -0.2469 terminal unrealized/realized ratio versus the selected budget125 profile of 104.68%, -21.17%, and -0.2273. A milder midpoint exit variant also stayed zero-margin on the 20k slice, but underperformed the tight variant before full-year promotion. Both temporary YAMLs were removed; raw logs were retained.

IDOL stress follow-up: a 25k-bar diagnostic on the selected `V21_idol_margin_zero_budget125.yaml` stayed zero-margin with shallow drawdown, but ended slightly negative because realized PnL was small relative to open loss. A bounded 25k-bar tuner pass improved that slice from -0.60% return, -2.00% MDD, and -2.6446 tail ratio to +0.10% return, -1.53% MDD, and -0.8798 tail ratio, all with zero margin calls. Full-year confirmation rejected the tuned profile: it stayed zero-margin, but worsened return, drawdown, and tail ratio versus the selected IDOL budget125 config. No IDOL YAML was promoted; the selected basket is unchanged.

SUP short-budget follow-up: trimming only SUP short-side budget from 32 to 28, with and without a small long-side increase to 34, stayed zero-margin on the 20k slice. Both probes worsened return, drawdown, and terminal unrealized/realized ratio versus selected `V21_sup_margin_zero_budget32_fast_exit.yaml`, so neither earned full-year confirmation. Temporary generated YAMLs were removed; raw logs were retained.

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
| temporary `V21_maxxing_margin_zero_exit_tight_nospacing.yaml` | 20000 | 31.19 | -7.83 | 1398 | 0 | 0 | -0.2905 | Promoted to full-year confirmation: exit-only transplant beat selected MAXXING budget125 on 20k return and MDD while staying zero-margin. | `_reports/akela_meta_short/margin_zero_codex_loop/maxxing_exit_tight_nospacing_20k.log` |
| temporary `V21_maxxing_margin_zero_exit_tight_nospacing.yaml` | full | 94.20 | -22.09 | 4422 | 0 | 0 | -0.2469 | Rejected after full-year: stayed zero-margin, but worse than selected MAXXING budget125's 104.68% return, -21.17% MDD, and -0.2273 tail ratio. Temporary YAML removed. | `_reports/akela_meta_short/margin_zero_codex_loop/maxxing_exit_tight_nospacing_full.log` |
| temporary `V21_maxxing_margin_zero_exit_mid.yaml` | 20000 | 28.27 | -8.24 | 1384 | 0 | 0 | -0.3097 | Rejected before full-year: stayed zero-margin and beat selected budget125 on the 20k slice, but underperformed the tighter exit-only variant selected for confirmation. Temporary YAML removed. | `_reports/akela_meta_short/margin_zero_codex_loop/maxxing_exit_mid_20k.log` |
| `V21_maxxing_margin_zero_budget125.yaml` | 25000 | -22.81 | -51.97 | 1321 | 0 | 0 | -1.5508 | Stress-window diagnosis: prior 20k screens missed the March 12 short squeeze; selected budget125 stayed zero-margin but had severe interim MTM/tail drag. | `_reports/akela_meta_short/margin_zero_codex_loop/maxxing_budget125_25k.log` |
| `V21_maxxing_margin_zero_shortcap100.yaml` | 25000 | -24.96 | -53.78 | 1218 | 0 | 0 | -1.6415 | Rejected before full-year: stayed zero-margin but worsened the stress slice; the cap did not reduce the problematic short unrealized loss. | `_reports/akela_meta_short/margin_zero_codex_loop/maxxing_shortcap100_25k.log` |
| `V21_maxxing_margin_zero_shortcap085.yaml` | 25000 | -24.96 | -53.78 | 1218 | 0 | 0 | -1.6415 | Rejected before full-year: identical stress-slice result to shortcap100, so the lower cap was not binding in this failure mode. | `_reports/akela_meta_short/margin_zero_codex_loop/maxxing_shortcap085_25k.log` |
| tuner final_best from `akela_margin_zero_maxxing_25k_stress_20260513_013417` | 25000 | -12.91 | -40.14 | 1423 | 0 | 0 | -1.2920 | Promoted to full-year confirmation: improved the expanded stress slice by tightening exits while preserving zero margin calls. | `_reports/akela_meta_short/margin_zero_codex_loop/maxxing_25k_stress_tuner_20260513.log` |
| `V21_maxxing_margin_zero_budget125_stress_exit.yaml` | full | 119.55 | -13.68 | 4996 | 0 | 0 | -0.2048 | Promoted as current MAXXING pick: full-year zero-margin and better than budget125 on return, drawdown, trades, and terminal unrealized ratio. | `_reports/akela_meta_short/margin_zero_codex_loop/maxxing_budget125_stress_exit_full.log` |
| `V21_idol_margin_zero_budget125.yaml` | 25000 | -0.60 | -2.00 | 350 | 0 | 0 | -2.6446 | Stress-window diagnosis: selected IDOL stayed zero-margin with shallow drawdown, but the slice ended with weak MTM because realized PnL was too small versus open loss. | `_reports/akela_meta_short/margin_zero_codex_loop/idol_budget125_25k.log` |
| tuner final_best from `akela_margin_zero_idol_25k_20260513_20260513_014913` | 25000 | 0.10 | -1.53 | 548 | 0 | 0 | -0.8798 | Promoted to full-year confirmation: improved the 25k slice return, MDD, and terminal unrealized ratio while preserving zero margin calls. | `_reports/akela_meta_short/margin_zero_codex_loop/idol_25k_tuner_20260513.log` |
| tuner final_best from `akela_margin_zero_idol_25k_20260513_20260513_014913` | full | 28.71 | -14.55 | 5847 | 0 | 0 | -0.4156 | Rejected after full-year: stayed zero-margin, but underperformed selected IDOL budget125 on return, drawdown, and terminal unrealized ratio. No YAML promoted. | `_reports/akela_meta_short/margin_zero_codex_loop/idol_25k_tuner_final_full.log` |
| temporary `V21_sup_margin_zero_l32_s28_fast_exit.yaml` | 20000 | 4.44 | -19.67 | 220 | 0 | 0 | -0.6452 | Rejected before full-year: short-side budget trim stayed zero-margin, but worsened return, MDD, and tail ratio versus selected SUP budget32_fast_exit. Temporary YAML removed. | `_reports/akela_meta_short/margin_zero_codex_loop/sup_l32_s28_fast_exit_20k.log` |
| temporary `V21_sup_margin_zero_l34_s28_fast_exit.yaml` | 20000 | 4.46 | -19.46 | 224 | 0 | 0 | -0.6439 | Rejected before full-year: small long-side budget increase plus short trim stayed zero-margin, but still underperformed selected SUP budget32_fast_exit on return, MDD, and tail ratio. Temporary YAML removed. | `_reports/akela_meta_short/margin_zero_codex_loop/sup_l34_s28_fast_exit_20k.log` |

## Baseline Comparison

| symbol | baseline return_mtm_% | baseline mdd_mtm_% | baseline margin_calls | margin-zero return_mtm_% | margin-zero mdd_mtm_% | margin-zero margin_calls |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `IDOL/USDT:USDT` | 44.19 | -37.75 | 18 | 34.70 | -13.40 | 0 |
| `FREEDOMMONEY/USDT:USDT` | 64.28 | -24.09 | 0 | 64.28 | -24.09 | 0 |
| `MAXXING/USDT:USDT` | 183.80 | -18.37 | 8 | 119.55 | -13.68 | 0 |
| `SUP/USDT:USDT` | -1.05 | -219.88 | 35 | 3.27 | -25.09 | 0 |

## Exact Full-Year Commands

```bash
python3 obw_platform/backtester_dual_long_short_fast_pack_v2.py --cfg obw_platform/meta_strategies/akela_meta_short/generated_configs/margin_zero/V21_idol_margin_zero_budget125.yaml --npz DB/akela_meta_short_1m_1y_idol_bingx.npz --symbol IDOL/USDT:USDT
```
```bash
python3 obw_platform/backtester_dual_long_short_fast_pack_v2.py --cfg obw_platform/configs/V21_freedommoney_bingx_live_candidate_1m_1y.yaml --npz DB/fast_cache_1m_freedommoney_1y_bingx.npz --symbol FREEDOMMONEY/USDT:USDT
```
```bash
python3 obw_platform/backtester_dual_long_short_fast_pack_v2.py --cfg obw_platform/meta_strategies/akela_meta_short/generated_configs/margin_zero/V21_maxxing_margin_zero_budget125_stress_exit.yaml --npz DB/fast_cache_1m_maxxing_1y_bingx.npz --symbol MAXXING/USDT:USDT
```
```bash
python3 obw_platform/backtester_dual_long_short_fast_pack_v2.py --cfg obw_platform/meta_strategies/akela_meta_short/generated_configs/margin_zero/V21_sup_margin_zero_budget32_fast_exit.yaml --npz DB/akela_meta_short_1m_1y_sup_bingx.npz --symbol SUP/USDT:USDT
```

## Next Action

First-basket margin-call cleanup remains satisfied with 0 total margin-call events using IDOL budget125, FREEDOMMONEY baseline, MAXXING budget125_stress_exit, and SUP budget32_fast_exit. The latest SUP short-budget probes did not improve the selected SUP risk/return balance. The next useful search is a bounded SUP tuner pass from `V21_sup_margin_zero_budget32_fast_exit.yaml` on a stress slice longer than 20k bars, because simple sizing changes inside this envelope are no longer producing improvements.
