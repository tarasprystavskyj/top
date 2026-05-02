# Task: Optimize DEX fast NPZ tuner v2

## Context

We collected quarterly CHECK/USDC Aerodrome CL events:

```text
Pool: 0x5a7b4970b2610aee4776a6944d9f2171ee6060b0
Period: 2026-02-01 → 2026-04-30
Swap events: 133474
Conservative fee_rate: 0.002515
Old slow replay best real-fee baseline:
  wide_80_5
  return: +21.4% quarterly
  MDD: -41.8%
```

Old replay had two problems:
1. fee share formula was too optimistic:
   old: our_liquidity / active_liquidity
   new: our_liquidity / (active_liquidity + our_liquidity)
2. adaptive fee accounting mixed reinvested and uncollected fees.

## New v2 files

```text
dex_platform/data_collectors/build_cl_fee_replay_npz_v2.py
dex_platform/backtest/cl_fee_replay_fast_npz_v2.py
dex_platform/scripts/build_check_2pct_fee_npz_v2.sh
dex_platform/scripts/run_check_2pct_fast_npz_april_v2.sh
dex_platform/scripts/tune_check_2pct_fast_npz_v2.sh
```

## Commands

```bash
cd /var/www/vps2.happyuser.info/top/top_1
unzip -o dex_fast_npz_tuner_bundle_v2.zip

chmod +x dex_platform/data_collectors/build_cl_fee_replay_npz_v2.py
chmod +x dex_platform/backtest/cl_fee_replay_fast_npz_v2.py
chmod +x dex_platform/scripts/build_check_2pct_fee_npz_v2.sh
chmod +x dex_platform/scripts/run_check_2pct_fast_npz_april_v2.sh
chmod +x dex_platform/scripts/tune_check_2pct_fast_npz_v2.sh

source /var/www/vps2.happyuser.info/top/backtest_SK/.venv38/bin/activate

bash dex_platform/scripts/build_check_2pct_fee_npz_v2.sh
bash dex_platform/scripts/run_check_2pct_fast_npz_april_v2.sh
bash dex_platform/scripts/tune_check_2pct_fast_npz_v2.sh
```

## Required logic

1. NPZ builder reads events_all.parquet or CSV and stores Swap arrays only.
2. Fast backtester loads NPZ once and filters by month in memory.
3. Correct fee share:
   `share = our_liquidity / (active_liquidity + our_liquidity)`
4. Report:
   - fees_earned_total
   - fees_reinvested
   - fees_uncollected_end
   - rebalance_costs
   - avg/max liquidity share
5. Penalize unrealistic liquidity share:
   - max share target: 5%
   - avg share target: 3%
6. Add walk-forward next:
   - train Feb → test Mar
   - train Mar → test Apr
   - train Feb+Mar → test Apr
7. Add multiprocessing worker initializer later:
   - load NPZ once per worker
   - pass only param tuples to workers.

## Success criteria

1. April-only backtest runs.
2. Tune outputs summary.csv sorted by score.
3. No result with max_liquidity_share > 5% is accepted when hard cap is on.
4. Results are comparable with slow replay, but lower if old formula overestimated fees.
