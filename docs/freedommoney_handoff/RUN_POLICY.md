# Run Policy

## Core rule

Do not optimize for pretty realized PnL. Optimize for MTM risk-adjusted return.

Score:

```text
primary_ratio = MTM_PnL / abs(MTM_DD_percent)
```

Secondary penalties:

```text
- hard reject margin_calls > 0
- penalize final_unrealized_abs > 60
- penalize trade count explosion
- penalize configs slower than baseline by >2x unless result is much better
```

## Session budget

Each Claude loop call should assume max 29 minutes. Use narrow guided sweeps, not huge brute-force grids.

## Preferred tuning dimensions

Allowed to tune:

- `tpPercent`
- `subSellTPPercent`
- `maxLongInvestPct`
- `maxShortInvestPct`
- DCA distance parameters if present in config
- trend/deleverage gates if already supported by strategy/backtester

Forbidden/static:

See `STATIC_LOCKS.md`.

## Stop conditions

Stop and report when:

- ratio > 15 with final unrealized abs < 60
- or ratio > 12 with MTM DD < 12 and final unrealized abs < 75
- or no improvement after 30 evaluated candidates

## No-go

Do not declare live-ready if final unrealized tail is hiding risk.
