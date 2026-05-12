# Champion bundle (optimizer_v3_8 backtester)

Self-contained reproduction package for FULL-PASS champion `ssp085_ssmf2` (wave imm38).
Backtester is from `optimizer_v3_8_ai_consilium_handoff_bundle`, NOT the active project's version.

## Important note on backtester divergence

Champion was optimized via the **active project's backtester** which produced **MTM = +15.92%**.
This bundle uses the **optimizer_v3_8 reference backtester**, which produces a **different number**
(measured: MTM ~ +3.27% on the same config + same NPZ).

Reason: the two backtesters handle partial fills (sub-sell / sub-cover) differently.
Bundle backtester executes ~60% of the trades that the active backtester executes for this config.

If you trust the bundle backtester as the reference, the realistic MTM for this champion is
**around +3.27% (paper)**, with **realized PnL +37%** (closed-trade dollars).

## Files

```
champion_bundle_v3_8_2026_05_10/
├── README.md                                       # this file
├── Dockerfile                                      # python 3.13 + numpy/pandas/yaml
├── docker-compose.yml                              # shell + backtest services
├── run.sh                                          # convenience runner
│
├── configs/
│   └── champion.yaml                               # FULL-PASS champion (ssp085_ssmf2)
│
├── strategies/
│   ├── cryptomine_pack_dual_compound.py            # CompoundLongPack + base
│   └── cryptomine_pack_dual_compound_v5.py         # CompoundShortPackV5 (dualMa)
│
├── backtester_dual_long_short_fast_pack_v2.py      # bundle CLI entry
├── backtester_dual_core_dynamic_v5.py              # bundle simulation engine
├── slippage_directional_model_v1.py                # slippage helper
├── slippage_orderbook_model_v1.py                  # slippage helper (optional)
│
└── data/
    └── ena_ohlcv_30s_1y_from_ticks.npz             # market data NPZ
```

## How to run

```bash
# One-shot backtest (12 months continuous)
./run.sh

# With monthly summary
./run.sh --monthly-summary /app/_reports/monthly.json

# Different time window
./run.sh --time-from 2025-07-01T00:00:00+00:00 --time-to 2025-09-01T00:00:00+00:00

# Interactive shell
docker compose run --rm shell
```

## Champion metrics (this bundle's backtester)

| Metric | Value | Note |
|---|---:|---|
| MTM | +3.27% | total_pnl_mtm / equity_start * 100 |
| realized | +$73.71 | +36.86% on $200 |
| MDD_mtm | -21.07% | OK |
| MC | 0 | OK |
| trades | 4982 | ~14/day |

Active-backtester reference numbers (for comparison):
- MTM: +15.92% (different, see "Backtester divergence" above)
- realized: +$92.12 (+46.06%)
- trades: 8375 (~23/day)

## Champion config summary

Strategy classes:
- Long: `strategies.cryptomine_pack_dual_compound.CompoundLongPack`
- Short: `strategies.cryptomine_pack_dual_compound_v5.CompoundShortPackV5`

Key mutables:
- Long: `compoundCapMult=1.8, compoundFactor=0.6, maxLongInvestPct=1.55, linearDropPercent=0.056, tpPercent=0.6, subSellTPPercent=0.9, hardBreakevenDeleveragePct=40`
- Short: `compoundCapMult=1.5, compoundFactor=0.7, maxShortInvestPct=0.8, linearRisePercent=0.08, tpPercent=0.5, subSellTPPercent=0.85, subSellMinFills=2, hardBreakevenDeleveragePct=40, dualMaEnabled=1`

Immutable parameters (from project rules):
- fee_rate=0.0005, funding=0, initial_equity=100, slippage_per_side=9.2387 (constant)
- equityForSizingUSDT=335, baseOrderPctEq=1.5, useEvenBars=0, useHighLowTouch=0.0
- timeframe=0.5m
