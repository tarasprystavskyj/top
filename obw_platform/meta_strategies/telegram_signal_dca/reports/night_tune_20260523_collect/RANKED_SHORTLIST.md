# Ranked Shortlist

Generated: 2026-05-23T19:43:22+00:00

Ranking key: primary return first, then net return, PF, max drawdown, and trade count. Binance rows use `net % max-cap` when present. Telegram rows use realized net PnL because the generated Telegram DCA summaries do not include max-cap normalized return.

## Shortlist

| rank | source | variant | horizon | primary metric | primary | net | MTM net | PF | maxDD | trades | win % | source file |
|---:|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | binance_copy:4751838302089254401 | dca3 | ttl_72h/reversal | net % max-cap | 4.08 | 20.40 | n/a | 1.214 | -34.42 | 50 | 50.0 | `binance_copy/4751838302089254401/summary.json` |
| 2 | binance_copy:4751838302089254401 | dca2 | ttl_72h/reversal | net % max-cap | 3.32 | 16.62 | n/a | 1.162 | -37.04 | 50 | 50.0 | `binance_copy/4751838302089254401/summary.json` |
| 3 | binance_copy:4751838302089254401 | dca1 | ttl_72h/reversal | net % max-cap | 2.94 | 14.68 | n/a | 1.139 | -38.01 | 50 | 50.0 | `binance_copy/4751838302089254401/summary.json` |
| 4 | binance_copy:4728671486012660992 | plain | ttl_72h/reversal | net % max-cap | 2.67 | 32.01 | n/a | 1.034 | -78.51 | 105 | 59.0 | `binance_copy/4728671486012660992/summary.json` |
| 5 | binance_copy:4751838302089254401 | plain | ttl_72h/reversal | net % max-cap | 1.79 | 8.94 | n/a | 1.082 | -38.76 | 50 | 48.0 | `binance_copy/4751838302089254401/summary.json` |
| 6 | binance_copy:4906010685108267264 | dca3 | ttl_72h/reversal | net % max-cap | 0.89 | 17.70 | n/a | 1.065 | -61.62 | 250 | 53.2 | `binance_copy/4906010685108267264/summary.json` |
| 7 | binance_copy:4906010685108267264 | dca2 | ttl_72h/reversal | net % max-cap | 0.61 | 12.29 | n/a | 1.040 | -64.81 | 250 | 49.6 | `binance_copy/4906010685108267264/summary.json` |
| 8 | binance_copy:4906010685108267264 | dca1 | ttl_72h/reversal | net % max-cap | 0.17 | 3.45 | n/a | 1.011 | -68.83 | 250 | 48.8 | `binance_copy/4906010685108267264/summary.json` |
| 9 | telegram:darkknighttrade | v21_dca3 | ttl_24h | realized net PnL | 0.04 | 0.04 | -5.59 | 1.002 | -2.40 | 21 | 81.0 | `scan_001/telegram/darkknighttrade/ttl_24h/darkknighttrade/all_signals/dca_summary.csv` |
| 10 | binance_copy:4906010685108267264 | plain | ttl_72h/reversal | net % max-cap | 0.01 | 0.17 | n/a | 1.001 | -72.22 | 250 | 48.4 | `binance_copy/4906010685108267264/summary.json` |
| 11 | telegram:darkknighttrade | v21_dca2 | ttl_24h | realized net PnL | -0.23 | -0.23 | -5.70 | 0.986 | -2.40 | 21 | 81.0 | `scan_001/telegram/darkknighttrade/ttl_24h/darkknighttrade/all_signals/dca_summary.csv` |
| 12 | telegram:darkknighttrade | v21_dca3 | ttl_48h | realized net PnL | -0.35 | -0.35 | -5.98 | 0.979 | -2.40 | 22 | 77.3 | `scan_001/telegram/darkknighttrade/ttl_48h/darkknighttrade/all_signals/dca_summary.csv` |

## Best By Source

| source | selected variant | horizon | primary metric | primary | net | PF | maxDD | trades | note |
|---|---|---|---|---:|---:|---:|---:|---:|---|
| binance_copy:4728671486012660992 | plain | ttl_72h/reversal | net % max-cap | 2.67 | 32.01 | 1.034 | -78.51 | 105 | candidate |
| binance_copy:4751838302089254401 | dca3 | ttl_72h/reversal | net % max-cap | 4.08 | 20.40 | 1.214 | -34.42 | 50 | candidate |
| binance_copy:4906010685108267264 | dca3 | ttl_72h/reversal | net % max-cap | 0.89 | 17.70 | 1.065 | -61.62 | 250 | candidate |
| telegram:darkknighttrade | v21_dca3 | ttl_24h | realized net PnL | 0.04 | 0.04 | 1.002 | -2.40 | 21 | candidate |

## Blocked Or Missing Data

| item | status | note |
|---|---|---|
| `telegram/Nevskiyh` | blocked | no parsed signals/symbols |
| `telegram/topslivs` | blocked | no parsed signals/symbols |
| `telegram/Treyding_Signaly_Kripto` | blocked | no parsed signals/symbols |

## Interpretation

- The strongest completed candidate is Binance lead `4751838302089254401` with `dca3`: +4.08% max-cap normalized return, PF 1.214, maxDD -34.42%, 50 trades.
- Binance lead `4728671486012660992` is only positive in `plain`; DCA variants are negative and have materially deeper drawdown.
- Binance lead `4906010685108267264` improves monotonically from plain to `dca3`, but the absolute max-cap return is small at +0.89% over 250 trades and drawdown remains large.
- Telegram `darkknighttrade` only turns slightly positive on realized PnL at `ttl_24h` + `v21_dca3`; all Telegram MTM net rows remain negative, so this is not a clean candidate yet.
- `Nevskiyh`, `topslivs`, and `Treyding_Signaly_Kripto` had no parsed symbols/signals in collection and therefore no tuneable result in this run.

## Limitations

- Stress slippage 18.7 bp is recorded as pending in the existing reports and is not represented in the completed comparison rows.
- Telegram and Binance metrics are not perfectly homogeneous: Binance exposes max-cap normalized returns; Telegram summaries expose realized/MTM PnL from the DCA comparison CSVs.
- This is a paper/backtest aggregation only. It does not place orders and does not inspect secrets.
