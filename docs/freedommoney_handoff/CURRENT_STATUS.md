# Current Status

## What was found

ENA 30s is structurally weak under realistic friction:

- fee per side: 0.0005
- slippage per side: 9.2387 bps
- round-trip cost: about 0.285%
- ENA 30s median range is far below that cost

This means ENA profits without slippage are mostly fake for this frequency.

## FREEDOMMONEY result on uploaded data

Best risk-ratio config found:

```text
Config: h4_freedommoney_ratio_best_v2.yaml
MTM PnL: +199.12 USDT
MTM DD: -15.69%
PnL/DD ratio: 12.69
realized: +295.23
final unrealized: -96.11
trades: 4409
margin calls: 0
```

Low-tail alternative:

```text
Config: h4_freedommoney_low_tail_expo_l10_s08.yaml
MTM PnL: +115.96
MTM DD: -21.33%
PnL/DD ratio: 5.44
final unrealized: -38.50
margin calls: 0
```

## Important warning

`best_v2` has a large final unrealized tail. It is not live-ready. Future work must reduce toxic inventory, not just maximize realized PnL.
