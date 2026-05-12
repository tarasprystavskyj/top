# Akela Basket Backtest Plan

Updated: 2026-05-12 UTC

## Purpose

Move from proxy ranking to evidence that the upper Akela selector can control a
lower short leg across a small basket.

## Candidate Basket

| Symbol | NPZ |
| --- | --- |
| `IDOL/USDT:USDT` | `DB/akela_meta_short_1m_1y_idol_bingx.npz` |
| `FREEDOMMONEY/USDT:USDT` | `DB/fast_cache_1m_freedommoney_1y_bingx.npz` |
| `MAXXING/USDT:USDT` | `DB/fast_cache_1m_maxxing_1y_bingx.npz` |
| `SUP/USDT:USDT` | `DB/akela_meta_short_1m_1y_sup_bingx.npz` |

## Allowed Baseline

Start with:

```text
obw_platform/configs/V21_freedommoney_bingx_live_candidate_1m_1y.yaml
```

This is a baseline for validation, not a production promotion. If it fails on a
symbol, record the failure instead of editing production configs.

## Allowed Backtester

```text
obw_platform/backtester_dual_long_short_fast_pack_v2.py
```

Use explicit `--npz` and `--symbol` on every run.

## Required Outputs

Write raw outputs under:

```text
_reports/akela_meta_short/basket_<UTC_STAMP>/
```

Write compact committed summaries under:

```text
obw_platform/meta_strategies/akela_meta_short/reports/latest_basket_summary.md
obw_platform/meta_strategies/akela_meta_short/reports/latest_basket_manifest.json
```

## Safety Rules

- Do not change exchange, fee, slippage, liquidation, or backtest math.
- Do not edit production strategy YAMLs.
- Do not treat a failed symbol as absent; include it in the basket report.
- Do not promote based on one symbol.
- If curves do not align, report per-symbol metrics and mark equal-weight
  basket curve unavailable.

## Success Criteria

The basket is interesting only if:

- all or most symbols run through the existing backtester;
- MTM risk-adjusted return is better than naive single-winner selection;
- drawdown/tail exposure is not concentrated in one symbol;
- the result has enough evidence to justify a focused tuner run.

## Champion Search Runner

Once all first-basket datasets are present, use:

```bash
./obw_platform/meta_strategies/akela_meta_short/run_yearly_champion_search.sh
```

This runner performs the yearly backtest matrix first, then launches sequential
night tuning for all first-basket symbols using the existing V21 1m 1y tuning
plan. It is the current bridge from Akela research to a paper-live candidate.
