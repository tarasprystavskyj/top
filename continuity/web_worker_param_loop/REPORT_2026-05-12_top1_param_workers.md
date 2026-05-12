# TOP1 web-worker parameter loop report

Run: 2026-05-12 20:09-20:28 UTC
Branch: local-webworkers-ui-admin
Parent agent: Marko1
Workers:
- worker_11: Parameter Hypothesis Scout
- worker_12: Parameter Critic and Robustness Auditor

## Result

The web-worker bridge worked: both workers received the context archive, both received the first follow-up task, and both returned two substantive responses. The 10-cycle run completed, but cycles 4-10 did not send new work because the worker manager entered rate-limit/backoff protection.

Observed cycle progress:
- Cycle 1: context archive uploaded and initial task sent to both workers.
- Cycle 2: both workers returned first responses; follow-up tasks were sent.
- Cycle 3: both workers returned second responses.
- Cycles 4-10: no new responses; backoff remained active.

Conclusion: the mechanism is usable, but the current loop cadence is too aggressive for a 10-cycle web-worker reasoning run. Future runs should use longer task gaps or treat one "cycle" as one completed response round rather than one polling pass.

Follow-up fix:
- `scripts/web_worker_param_loop.py` now separates polling attempts from completed reasoning cycles.
- Rate-limit/backoff waits no longer increment the logical cycle counter.
- The orchestrator can create runtime-only candidate YAMLs, run bounded local backtests, and include a concise backtest artifact summary in the next context archive.

## Consensus ranking

1. FREEDOMMONEY H4 hybrid tail-compression, based on `obw_platform/configs/h4_freedommoney_hybrid_balanced_v3.yaml`.
2. FREEDOMMONEY V21 de-tail / exposure-compression pass.
3. SUP V21 drawdown-repair pass.
4. ENA V21 narrow defensive retest.
5. MAXXING V21 repair-only pass, lower priority because of squeeze/drawdown risk.

Rejected or deferred:
- IDOL: useful as a basket-observation candidate, but not first-budget parameter tuning.
- `h4_freedommoney_ratio_best_v2.yaml`: strong ratio, but terminal unrealized risk is too toxic for direct uplift.

## Concrete candidate set

Primary H4 lane:

Base: `obw_platform/configs/h4_freedommoney_hybrid_balanced_v3.yaml`

Known baseline cited by workers:
- MTM: +166.67
- MDD: -20.39%
- final unrealized: -39.93
- trades: 4042
- margin calls: 0

Test first:

```yaml
# w11c2_fm_h4_A_tail_safe
strategy_params_long.maxLongInvestPct: 1.0
strategy_params_long.tpPercent: 0.85
strategy_params_long.subSellTPPercent: 1.35
strategy_params_short.maxShortInvestPct: 1.05
strategy_params_short.tpPercent: 0.65
strategy_params_short.subSellTPPercent: 1.56
```

Worker rationale: avoid raising long exposure because `ratio_best_v2` already showed that profit-chasing can recreate toxic terminal inventory. Try modest short-side lift and lower long sub-sell TP first.

## First Local Backtest Pass

Dataset: `DB/fast_cache_akela_shortlist_1m_30d.npz`

| rank | candidate | MTM | MDD MTM | unrealized | trades | margin calls | verdict |
|---:|---|---:|---:|---:|---:|---:|---|
| 1 | `h4_freedommoney_hybrid_balanced_v3` baseline | 166.67 | -20.39% | -39.93 | 4042 | 0 | still best |
| 2 | `fm_h4_w11c2_A_tail_safe_30d` | 163.52 | -20.09% | -39.96 | 4208 | 0 | slightly lower DD, worse MTM |
| 3 | `fm_h4_w11_range_mid_tail_compression_30d` | 152.78 | -20.02% | -39.96 | 3980 | 0 | worse MTM |
| 4 | `sup_v21_dd_repair_exposure_spacing_30d` | 27.74 | -26.81% | -2.22 | 1641 | 22 | rejected until margin calls fixed |
| 5 | `fm_v21_det_tail_exposure_compress_30d` | -31.80 | -38.59% | -107.08 | 7740 | 46 | rejected |

Result: the worker guesses did not yet beat the FreedomMoney H4 baseline on the available 30d dataset. The useful signal is that `w11c2_A` reduced drawdown slightly but paid too much MTM for it; the next worker task should search for a smaller tail-reduction move or explain why the baseline is already near the local optimum.

Secondary V21 lane:

Base candidates:
- `obw_platform/configs/V21_freedommoney_bingx_live_candidate_1m_1y.yaml`
- `obw_platform/configs/V21_current_best_tuner_freedommoney_bingx_1m_1y_20260511.yaml`

Direction:
- compress `maxLongInvestPct` before chasing higher TP
- widen DCA spacing if drawdown/tail dominates
- preserve fee/slippage/liquidation/backtest math

SUP lane:
- Treat as drawdown-repair, not yield-max.
- Compress exposure and widen spacing.
- Do not increase TP until drawdown improves.

ENA lane:
- Use only narrow defensive retests around V21 static9p38.
- Do not revive global `long.subSellTPPercent: 0.43`; worker_12 rejected it as failed 1y robustness.

## Run artifacts

Runtime artifacts remain local and ignored:
- `continuity/web_worker_param_loop/runtime/web_worker_param_loop.log`
- `continuity/web_worker_param_loop/runtime/web_worker_param_loop_state.json`
- `continuity/web_worker_param_loop/runtime/ui_data/web_worker_param_tree.json`
- `continuity/web_worker_param_loop/runtime/top1_web_worker_context.zip`
- `.playwright-mcp/`
