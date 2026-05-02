# DEX fast NPZ tuner bundle v2

Full files, not patches.

Main fixes:
- Correct fee share:
  `our_liquidity / (active_liquidity + our_liquidity)`
- Fees accounting split:
  `fees_earned_total`, `fees_reinvested`, `fees_uncollected_end`, `rebalance_costs`
- Liquidity-share cap and scoring.
- Month filter.

Quick run:

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
