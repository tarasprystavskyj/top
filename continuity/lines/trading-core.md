# trading-core

## Stage

plant

## What This Line Is

Core research and backtest execution in `obw_platform/`: the backtester family, strategy engine assumptions, config loading, order/fill semantics, LIFO lot behavior, realized/MTM accounting, and the decision about which engine is currently canonical.

## Current Shape

The repo contains multiple generations of backtesters and engines: `backtester_core_*`, `backtester_dual_*`, `backtester_dual_long_short_*`, `engine/*`, tuner scripts, strategy configs, and tests. `UI/backend/api_main.py` also has logic around selectable backtesters, so engine choice already exists in more than one place.

The project’s current risk is not lack of code. The risk is that several engines can look plausible while only one is actually meant to represent the live strategy.

## What Matters

- One active strategy family must have one canonical backtester.
- LIFO lot closure rules must remain exact.
- Realized PnL and MTM equity are separate metrics.
- Fees, slippage, funding, and exchange mechanics cannot be “added later” if the result is meant for live confidence.
- YAML config compatibility is part of the engine contract, not a convenience detail.

## Drift Risks

- Tuning one engine and launching another.
- Letting wrapper scripts become the real entrypoint without saying so.
- Mixing research-only assumptions into live-facing workflows.
- Breaking Pine/Python/live parity through small fill-timing changes.
- Reporting only realized PnL while the strategy has unacceptable MTM drawdown.
- Changing YAML defaults silently.

## Next Useful Move

Declare one canonical backtester for each active strategy family and record one smoke command per family next to the chosen config.
