# Signal Source MTM Portfolio

- Total capital: $500.00
- Mode: DCA only, one directional leg per signal
- Base DCA order: 5.00% of delegated source capital
- Portfolio final MTM: $513.05 (+2.61%)
- Portfolio MTM max drawdown: -1.35%
- Portfolio extrapolated return per 30d: +0.62%

## Allocation

| Source | Variant | Allocation | Base order | Score | 30d % | MTM MDD % | End % | Signals |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| tg:Nevskiyh | v21_dca5 | $128.17 (25.6%) | $6.41 | 0.2990 | +1.54% | -2.82% | +3.24% | 30 |
| tg:topslivs | v21_dca2 | $113.73 (22.7%) | $5.69 | 0.2653 | +0.79% | -2.59% | +1.66% | 75 |
| binance:4728671486012660992_20260519 | dca3 | $107.30 (21.5%) | $5.37 | 0.2503 | +0.34% | -1.34% | +1.44% | 100 |
| binance:4906010685108267264_20260519 | dca3 | $101.00 (20.2%) | $5.05 | 0.2356 | +0.58% | -2.48% | +2.43% | 358 |
| binance:4751838302089254401_20260519_ttl72_baseline | dca3 | $38.11 (7.6%) | $1.91 | 0.0889 | +1.96% | -15.09% | +7.88% | 47 |
| tg:Treyding_Signaly_Kripto | v21_dca5 | $11.69 (2.3%) | $0.58 | 0.0273 | +0.05% | -0.71% | +0.11% | 13 |
| tg:White_Ghosto | v21_dca5 | $0.00 (0.0%) | $0.00 | 0.0000 | -0.71% | -2.29% | -1.50% | 15 |
| tg:kriptaw | v21_dca5 | $0.00 (0.0%) | $0.00 | 0.0000 | -0.71% | -2.29% | -1.50% | 15 |

## Files

- `signal_sources_best_variants_mtm.png`
- `portfolio_500_ranked_mtm_canvas.png`
- `portfolio_500_ranked_mtm.csv`
- `source_allocations.csv`

Note: Telegram curves use existing MTM equity CSV. Binance-copy curves reconstruct MTM from saved 1m candles and trade fills where available.
Sizing note: historical curves are scaled linearly to delegated capital; `base_order_usd` is the intended live/paper DCA initial order size.
