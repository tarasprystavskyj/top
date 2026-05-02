# strategy-research

## Stage

sprout

## What This Line Is

The research layer for finding, tuning, and promoting candidate strategies: tuner plans, symbol scans, walk-forward tests, short/long leg experiments, DEMA/pack variants, and classifier ideas.

## Current Shape

The repo has many tuner plans, generated reports, candidate YAMLs, old scripts, universe files, and multi-agent task artifacts. That is normal for research, but it becomes dangerous when an experiment is treated as production without a promotion path.

## What Matters

- Every candidate needs the same benchmark ladder: smoke, month, quarter, rolling year.
- Results must include realized PnL, MTM MDD, realized MDD, fees, turnover, trades, and margin-call count.
- A high return on one symbol or one window is not a strategy.
- A classifier/symbol selector must be validated out-of-sample and must not learn from the target window.
- A strategy can be an indicator only if its signal is measured with strict time causality.

## Drift Risks

- Overfitting one month and calling it a robust edge.
- Comparing candidates run on different cache vintages.
- Optimizing headline annual return while ignoring MTM drawdown.
- Forgetting funding costs.
- Moving from short-only logic to dual-leg logic without testing leg handoff windows.
- Keeping old winners alive when they fail the current validation ladder.

## Next Useful Move

Create a single promotion table: candidate config, cache used, time window, month result, quarter result, yearly-by-month result, MTM MDD, realized MDD, fees, turnover, verdict.
