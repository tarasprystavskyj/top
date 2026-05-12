# Agent State

Last iteration: 2026-05-11 - Resume revalidation on FREEDOMMONEY

## 2026-05-11 Resume Results

Workspace and `/tmp` are writable again. Disk check showed about 6.1G free on `/`, `/tmp`, and the project mount.

Actions executed:
- Created `universe/universe_FREEDOMMONEY_1m.txt` for single-symbol extended data collection.
- Tried to fetch 90d 1m BingX futures OHLCV into `DB/freedommoney_bingx_1m_90d.npz`.
- Fetch failed before writing data because DNS/network is unavailable for `open-api.bingx.com` (`Name or service not known`).
- Revalidated local 30d Akela cache and saved fresh outputs under `_reports/freedommoney/resume_20260511_*`.
- Static lock check passed for `h4_freedommoney_hybrid_balanced_v3.yaml`.

Fresh local revalidation on `DB/fast_cache_akela_shortlist_1m_30d.npz`:
- `h4_freedommoney_hybrid_balanced_v3.yaml`: ratio 8.17, MTM +166.67, DD -20.39%, unrealized -39.93, margin calls 0, constraint OK.
- `h4_freedommoney_ratio_best_v2.yaml`: ratio 12.69, MTM +199.12, DD -15.69%, unrealized -96.11, margin calls 0, rejected by -60 unrealized constraint.
- `h4_c002_w9_fee_0002.yaml`: ratio 7.70, MTM +91.43, DD -11.87%, unrealized -48.35, margin calls 0, baseline OK.

Current decision remains: `h4_freedommoney_hybrid_balanced_v3.yaml` is the best constraint-respecting candidate on available local data.

Next concrete action: rerun the 90d fetch from a shell with outbound DNS/network, then revalidate `hybrid_v3`, `ratio_best_v2`, and baseline on `DB/freedommoney_bingx_1m_90d.npz`.

## Iteration 2 Results

Tested 5 new/existing configs on fast_cache_akela_shortlist_1m_30d.npz (FREEDOMMONEY symbol).

**New Winner Candidate: h4_freedommoney_hybrid_balanced_v3.yaml**
- Ratio: 8.17
- MTM PnL: +166.67
- MTM DD: -20.39%
- Unrealized: -39.93 ✓ (meets -60 constraint)
- Margin Calls: 0

**Previous best (constraint violation):**
- `h4_freedommoney_ratio_best_v2.yaml`
- Ratio: 12.69 (higher)
- MTM +199.12
- DD -15.69%
- Unrealized: -96.11 ✗ (exceeds -60 USDT constraint)

**Alternative (conservative):**
- `h4_freedommoney_low_tail_expo_l10_s08.yaml`
- Ratio: 5.44
- MTM +115.96
- DD -21.33%
- Unrealized: -38.50

## Hybrid v3 Tuning

Lower long TP and capped long investment to reduce underwater position:
```yaml
Long:  tpPercent 0.85 (was 1.1), subSellTPPercent 1.5 (was 1.815), maxLongInvestPct 1.0 (was 2.0)
Short: tpPercent 0.65 (unchanged), subSellTPPercent 1.56 (unchanged)
```

## Next Steps

1. Deploy hybrid_v3 as constraint-respecting candidate
2. Option: Collect extended FREEDOMMONEY data (90+ days) to validate stability
3. Option: Further tune to push ratio toward 10+ while maintaining tail < 60
