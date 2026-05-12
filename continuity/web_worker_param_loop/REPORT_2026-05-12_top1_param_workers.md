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

