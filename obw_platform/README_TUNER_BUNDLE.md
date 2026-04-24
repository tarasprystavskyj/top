# ENA fee-aware LIFO tuner bundle

Overlay this bundle on top of the repository root (`obw_platform` or equivalent):

```bash
cp -r tuner_bundle_feeaware_logged_v1/* /path/to/repo/
cd /path/to/repo
```

Smoke test:

```bash
bash run_smoke_tuner.sh /path/to/ena_ohlcv_30s_1y_from_ticks.npz
```

Long risk-reduction run:

```bash
python3 auto_tuner_dual_fast_pack.py \
  --cfg configs/final_best_ena_feeaware_logged_v1.yaml \
  --npz /path/to/ena_ohlcv_30s_1y_from_ticks.npz \
  --plan tuner_plans/tuner_plan_ENA_risk_reduction_quarter.py \
  --time-from 2025-12-15T00:00:00+00:00 \
  --time-to   2026-03-15T00:00:00+00:00 \
  --jobs 28 \
  --min-trades 200 \
  --w-pnl 1.0 \
  --w-mdd 180.0 \
  --w-realized-mdd 5.0 \
  --prefix ENA_risk_reduction_q
```

The tuner writes:
- `_reports/_auto_tuner_dual_fast_pack/<plan>/<session>/tuner_log.csv`
- `tuner_top20.csv`
- `final_best.yaml`
- `tuner_summary.json`

## Interpretation

For cutting MTM MDD, raise `w_mdd` and prefer the risk plan. For profit search, use `tuner_plan_ENA_profit_balanced_quarter.py`.

## Main MDD levers

1. Lower `hardBreakevenDeleveragePct` to trigger breakeven deleverage earlier.
2. Increase grid spacing: `linearDropPercent`, `linearRisePercent`, `drop/rise1..4`.
3. Reduce convexity: `mult2`, `mult4`, `mult5`.
4. Reduce burstiness: `maxFillsPerBar`, `maxOrdersPer3Min`.
5. Reduce trend max sizing: `maxLongInvestPct`, `maxShortInvestPct`.
6. Lower `tpPercent` / `subSellTPPercent` to exit stuck inventory sooner, at the cost of average profit per exit.
