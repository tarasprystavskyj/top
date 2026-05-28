ENA second-leg 30s collection/ranking workflow is ready for review.

Repo: C:\python_scripts\top_1
Branch: demoquantfut-ena-second-leg-data

Inputs:
- ENA NPZ: DB/ena_ohlcv_30s_1y_from_ticks_compat_np1.npz
- Candidate NPZ glob: DB/*30s*1y*.npz
- Candidate universe: obw_platform/universe/universe_ena_second_leg_candidates.txt

Outputs:
- Ranking markdown: docs\ena_second_leg_data\reports\ena_second_leg_rank.md
- Ranking CSV: docs\ena_second_leg_data\reports\ena_second_leg_rank.csv
- Ranking manifest: docs\ena_second_leg_data\reports\ena_second_leg_rank_manifest.json

Please inspect the reports, identify the best practical second leg for ENA stat-arb, and update the runbook/recommendation if the collected data is sufficient. If ranked_candidates is zero, explain which candidate data is still missing and the next safe collection command.
