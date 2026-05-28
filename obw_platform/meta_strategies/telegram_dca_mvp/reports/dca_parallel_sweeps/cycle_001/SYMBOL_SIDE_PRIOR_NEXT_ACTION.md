# Symbol/Side Prior-Only Next Action

Date: 2026-05-16
Mode: paper-only research
Live readiness: false

## Source

Source run:
`obw_platform/meta_strategies/telegram_dca_mvp/reports/dca_parallel_sweeps/cycle_001/all_49__adds1__cap2x__tp1__wedge`

Input:
`telegram_dca_trades.csv`

Outputs:
- `symbol_side_prior_analysis/symbol_contribution_best_dca.csv`
- `symbol_side_prior_analysis/symbol_side_contribution_best_dca.csv`
- `symbol_side_prior_analysis/prior_only_selector_check_best_dca.csv`
- `symbol_side_prior_analysis/symbol_side_prior_analysis.md`

## Findings

Worst symbols by DCA PnL contribution:

```text
RENDER  trades=24 pnl=-3.469652%
PYTH    trades=9  pnl=-2.086031%
ROSE    trades=5  pnl=-2.047546%
GRT     trades=3  pnl=-0.849242%
ONDO    trades=7  pnl=-0.716785%
BCH     trades=5  pnl=-0.645727%
OP      trades=7  pnl=-0.583969%
LINK    trades=10 pnl=-0.563477%
ICP     trades=5  pnl=-0.557414%
AAVE    trades=10 pnl=-0.524330%
```

Best symbols by DCA PnL contribution:

```text
SUI   trades=18 pnl=0.434678%
DOT   trades=13 pnl=0.364429%
INJ   trades=12 pnl=0.288245%
ADA   trades=9  pnl=0.245536%
ORDI  trades=5  pnl=0.239294%
JUP   trades=6  pnl=0.206083%
FET   trades=7  pnl=0.205529%
LDO   trades=6  pnl=0.199763%
GALA  trades=8  pnl=0.193904%
NEAR  trades=7  pnl=0.163360%
```

Prior-only selector checks on the DCA trade stream:

```text
all_trades_control: selected=256 pnl=-9.804703% mdd=-9.960271%
prior_symbol_min3_positive: selected=86 pnl=-3.056912% mdd=-3.482445%
prior_symbol_min5_positive: selected=51 pnl=-0.129466% mdd=-0.717990%
prior_symbol_side_min3_positive: selected=59 pnl=-1.161927% mdd=-2.677847%
prior_symbol_side_min5_positive: selected=24 pnl=0.053578% mdd=-0.609247%
```

## Interpretation

`prior_symbol_side_min5_positive` is the only positive diagnostic selector, but it has only 24 selected trades. It is not promotable.

The next safe test is not another broad all_49 DCA grid. The next safe test is a small paper-only filter run using:

- `prior_symbol_min5_positive` because it has 51 selected trades and nearly flat PnL.
- `prior_symbol_side_min5_positive` only as diagnostic because it has 24 selected trades.
- Mandatory `all_49` control.
- Mandatory no-oracle statement.

## Next Bounded Command Objective

Create prior-only filtered signal CSVs from the DCA trade stream and original signal CSV, then run the same representative DCA configs used in `commands_filters.ps1`.

Required filters:

```text
prior_symbol_min5_positive
prior_symbol_side_min5_positive_diagnostic
prior_symbol_min5_positive_excluding_worst_symbols
```

Stop rule:

Do not promote any result with fewer than 50 opened trades, and do not treat symbol lists from full-sample contribution as deployable without walk-forward/prior-only construction.
