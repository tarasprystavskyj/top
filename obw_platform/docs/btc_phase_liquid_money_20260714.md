# BTC-Phase Liquid-Money Direction

Research/paper-only branch for the BTC movement phase allocator over
liquid-money sleeves.

## Safety

- Paper/research only.
- No Dhan connection.
- No live exchange orders.
- No webhooks.
- No credential, cookie, browser profile, or local-storage reads.

## Hypothesis

Treat BTC price as a sampled function `x(t)` and allocate capital based on
finite-difference operators over that function:

```text
D x   = BTC velocity / momentum
D2 x  = BTC acceleration / momentum change
phase = region in the (D x, D2 x) phase plane
```

For day `t`, the allocator only uses the BTC phase known at the close of day
`t-1`. It then looks back over recent same-phase history and assigns weights to
liquid-money sleeves by a volatility-adjusted response score.

## Current Research Champion

Local artifact:

```text
obw_platform/_reports/btc_phase_dynamic_weights_20260714_v3_capfixed
```

No-lookahead cap-fixed champion:

```text
btc_phase_lb30_b4_cap0.6_sm0_equal
total: +1518.55%
MDD: -10.36%
stagnation: 21d
```

Static five-asset grid baseline over the same sleeves picked 100% HYPE:

```text
total: +230.28%
MDD: -12.93%
stagnation: 39d
```

## Paper Config

```text
obw_platform/configs/cfg_liquid_money_btc_phase_5asset_paper_20260714.yaml
```

`BTC/USDT:USDT` is used as an informational symbol. The traded sleeves are:

```text
PYTH/USDT:USDT
HYPE/USDT:USDT
SOL/USDT:USDT
ENA/USDT:USDT
UNI/USDT:USDT
```

The current paper config uses 1x long and 1x short leverage, a 2.5 USDT minimum
order, and the existing liquid-money stubborn inner adapter for entries and
exits.

## Next Validation

1. Tune ENA and UNI as native sleeves instead of borrowing HYPE/SOL templates.
2. Run walk-forward with embargo folds over the BTC-phase allocator.
3. Compare paper-live MTM against no-lookahead backtest expectations.
4. Only after those gates, consider an AI/news layer `N(t)` above the market
   layer `(D x, D2 x)`.
