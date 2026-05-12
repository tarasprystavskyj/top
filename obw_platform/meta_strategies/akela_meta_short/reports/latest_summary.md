# Akela Meta Short Latest Summary

Updated: 20260512T113636Z
Phase dataset: `DB/fast_cache_akela_shortlist_1m_30d.npz`
Short-leg dataset: `DB/akela_top200_1m_30d.db`
Raw artifacts: `_reports/akela_meta_short/20260512T113636Z`

## Job Results

| job | returncode | seconds | log |
| --- | --- | ---: | --- |
| phase_proxy_rank:baseline | 0 | 15.91 | `_reports/akela_meta_short/20260512T113636Z/phase_proxy_rank:baseline.log` |
| monthly_rolling_phase_proxy:baseline | 0 | 24.98 | `_reports/akela_meta_short/20260512T113636Z/monthly_rolling_phase_proxy:baseline.log` |
| short_leg_rank_no_backtest:baseline | 0 | 20.97 | `_reports/akela_meta_short/20260512T113636Z/short_leg_rank_no_backtest:baseline.log` |
| phase_proxy_rank:sensitive_failed_pump | 0 | 18.07 | `_reports/akela_meta_short/20260512T113636Z/phase_proxy_rank:sensitive_failed_pump.log` |
| monthly_rolling_phase_proxy:sensitive_failed_pump | 0 | 27.71 | `_reports/akela_meta_short/20260512T113636Z/monthly_rolling_phase_proxy:sensitive_failed_pump.log` |
| short_leg_rank_no_backtest:sensitive_failed_pump | 0 | 19.84 | `_reports/akela_meta_short/20260512T113636Z/short_leg_rank_no_backtest:sensitive_failed_pump.log` |
| phase_proxy_rank:strict_late_decay | 0 | 17.99 | `_reports/akela_meta_short/20260512T113636Z/phase_proxy_rank:strict_late_decay.log` |
| monthly_rolling_phase_proxy:strict_late_decay | 0 | 25.29 | `_reports/akela_meta_short/20260512T113636Z/monthly_rolling_phase_proxy:strict_late_decay.log` |
| short_leg_rank_no_backtest:strict_late_decay | 0 | 20.62 | `_reports/akela_meta_short/20260512T113636Z/short_leg_rank_no_backtest:strict_late_decay.log` |

## Repeated Candidates Across Profiles

- `IDOL/USDT:USDT` appears in 9 reports: phase:baseline, monthly:baseline, short_leg:baseline, phase:sensitive_failed_pump, monthly:sensitive_failed_pump, short_leg:sensitive_failed_pump, phase:strict_late_decay, monthly:strict_late_decay, short_leg:strict_late_decay
- `4/USDT:USDT` appears in 6 reports: phase:baseline, monthly:baseline, phase:sensitive_failed_pump, monthly:sensitive_failed_pump, phase:strict_late_decay, monthly:strict_late_decay
- `BEAT/USDT:USDT` appears in 6 reports: phase:baseline, monthly:baseline, phase:sensitive_failed_pump, monthly:sensitive_failed_pump, phase:strict_late_decay, monthly:strict_late_decay
- `CYS/USDT:USDT` appears in 6 reports: phase:baseline, monthly:baseline, phase:sensitive_failed_pump, monthly:sensitive_failed_pump, phase:strict_late_decay, monthly:strict_late_decay
- `DRIFT/USDT:USDT` appears in 6 reports: phase:baseline, monthly:baseline, phase:sensitive_failed_pump, monthly:sensitive_failed_pump, phase:strict_late_decay, monthly:strict_late_decay
- `FREEDOMMONEY/USDT:USDT` appears in 6 reports: phase:baseline, monthly:baseline, phase:sensitive_failed_pump, monthly:sensitive_failed_pump, phase:strict_late_decay, monthly:strict_late_decay
- `KOMA/USDT:USDT` appears in 6 reports: phase:baseline, monthly:baseline, phase:sensitive_failed_pump, monthly:sensitive_failed_pump, phase:strict_late_decay, monthly:strict_late_decay
- `MAXXING/USDT:USDT` appears in 6 reports: phase:baseline, monthly:baseline, phase:sensitive_failed_pump, monthly:sensitive_failed_pump, phase:strict_late_decay, monthly:strict_late_decay
- `PIPPIN/USDT:USDT` appears in 6 reports: phase:baseline, monthly:baseline, phase:sensitive_failed_pump, monthly:sensitive_failed_pump, phase:strict_late_decay, monthly:strict_late_decay
- `PLAYSOUT/USDT:USDT` appears in 6 reports: phase:baseline, monthly:baseline, phase:sensitive_failed_pump, monthly:sensitive_failed_pump, phase:strict_late_decay, monthly:strict_late_decay
- `SUP/USDT:USDT` appears in 6 reports: phase:baseline, monthly:baseline, phase:sensitive_failed_pump, monthly:sensitive_failed_pump, phase:strict_late_decay, monthly:strict_late_decay
- `TESTICLE/USDT:USDT` appears in 6 reports: phase:baseline, monthly:baseline, phase:sensitive_failed_pump, monthly:sensitive_failed_pump, phase:strict_late_decay, monthly:strict_late_decay
- `AIOT/USDT:USDT` appears in 3 reports: short_leg:baseline, short_leg:sensitive_failed_pump, short_leg:strict_late_decay
- `BIO/USDT:USDT` appears in 3 reports: short_leg:baseline, short_leg:sensitive_failed_pump, short_leg:strict_late_decay
- `BTC/USDT:USDT` appears in 3 reports: phase:baseline, phase:sensitive_failed_pump, phase:strict_late_decay
- `DOLO/USDT:USDT` appears in 3 reports: short_leg:baseline, short_leg:sensitive_failed_pump, short_leg:strict_late_decay
- `ETH/USDT:USDT` appears in 3 reports: phase:baseline, phase:sensitive_failed_pump, phase:strict_late_decay
- `FLOCK/USDT:USDT` appears in 3 reports: short_leg:baseline, short_leg:sensitive_failed_pump, short_leg:strict_late_decay
- `M/USDT:USDT` appears in 3 reports: short_leg:baseline, short_leg:sensitive_failed_pump, short_leg:strict_late_decay
- `MEME/USDT:USDT` appears in 3 reports: short_leg:baseline, short_leg:sensitive_failed_pump, short_leg:strict_late_decay

## Yearly Data Plan

- `IDOL`: present, bars=482080, action=skip, target=`DB/akela_meta_short_1m_1y_idol_bingx.npz`
- `FREEDOMMONEY`: present, bars=104184, action=skip, target=`DB/fast_cache_1m_freedommoney_1y_bingx.npz`
- `MAXXING`: present, bars=108569, action=skip, target=`DB/fast_cache_1m_maxxing_1y_bingx.npz`
- `SUP`: present, bars=251806, action=skip, target=`DB/akela_meta_short_1m_1y_sup_bingx.npz`

## Phase Proxy Top Rows

| symbol | final_phase_short_score | proxy_return_total_pct | proxy_mdd_mtm_pct | ret_total_pct |
| --- | --- | --- | --- | --- |
| MAXXING/USDT:USDT | 6.21875 | 93.83261571245156 | -95.6577762144973 | 3.4793814432989567 |
| FREEDOMMONEY/USDT:USDT | 5.862613048884236 | 190.0107563989246 | -46.45608406917938 | -29.548140740067286 |
| SUP/USDT:USDT | 5.467678571428571 | 55.59904117715608 | -58.45458153565091 | -21.10266159695817 |
| TESTICLE/USDT:USDT | 4.766090464104423 | 38.52524899748555 | -107.45467945947469 | -25.185185185185176 |
| IDOL/USDT:USDT | 4.618035714285714 | 18.62602453719513 | -25.674895506625607 | -11.477272727272736 |
| BEAT/USDT:USDT | 3.9749416709928704 | -28.06770254475518 | -75.0004781316525 | -12.656467315716268 |
| PIPPIN/USDT:USDT | 3.779662060052173 | 57.836723393956376 | -52.7862060335219 | -69.39972278494508 |
| KOMA/USDT:USDT | 3.6328571428571426 | -36.591413841742806 | -144.11590817364657 | 24.273127753303967 |
| CYS/USDT:USDT | 3.3367731573466797 | -80.33479061719478 | -129.22349777955304 | -19.000570017100504 |
| DRIFT/USDT:USDT | 3.282006395503457 | 7.08854574220689 | -104.12716368574027 | -59.71664698937427 |

## Monthly Stability Top Rows

| symbol | portfolio_score | months_tested | positive_rate | median_proxy_return_total_pct |
| --- | --- | --- | --- | --- |
| SUP/USDT:USDT | 3.370833333333333 | 3 | 1.0 | 34.81006629176765 |
| FREEDOMMONEY/USDT:USDT | 3.2530650081849295 | 4 | 1.0 | 61.65435436199867 |
| MAXXING/USDT:USDT | 3.025 | 4 | 1.0 | 20.001081943433718 |
| BEAT/USDT:USDT | 2.783333333333333 | 3 | 0.6666666666666666 | 52.352857529939975 |
| PIPPIN/USDT:USDT | 2.3625000000000003 | 4 | 0.75 | 16.62588275110727 |
| IDOL/USDT:USDT | 2.316666666666667 | 4 | 0.5 | 4.231830331504763 |
| CYS/USDT:USDT | 2.2375000000000003 | 3 | 0.6666666666666666 | 45.557347906186195 |
| KOMA/USDT:USDT | 1.9 | 2 | 0.5 | -1.4765175560893704 |
| TESTICLE/USDT:USDT | 1.6310729698893793 | 4 | 0.5 | 3.1568835451527173 |
| PLAYSOUT/USDT:USDT | 1.2375 | 3 | 0.3333333333333333 | -10.141025646838102 |

## Short Leg Rank Top Rows

| symbol | final_short_score | rel_total_pct | ret_total_pct | market_total_pct |
| --- | --- | --- | --- | --- |
| AIOT/USDT:USDT | 5.790384615384614 | 548.7259227715093 | 545.5190771960957 | -3.2068455754135305 |
| TA/USDT:USDT | 4.844145506474968 | -12.325594481003954 | -15.532440056417485 | -3.2068455754135305 |
| IDOL/USDT:USDT | 4.734578910899948 | 0.1089521429476159 | -3.0978934324659146 | -3.2068455754135305 |
| DOLO/USDT:USDT | 4.340105151897296 | 9.29223114163964 | 6.085385566226109 | -3.2068455754135305 |
| MEME/USDT:USDT | 3.977500572234843 | 4.622774778953365 | 1.415929203539834 | -3.2068455754135305 |
| BIO/USDT:USDT | 3.807268328310256 | 77.36480144068646 | 74.15795586527294 | -3.2068455754135305 |
| FLOCK/USDT:USDT | 3.5711487280261625 | 27.172555601503714 | 23.965710026090182 | -3.2068455754135305 |
| NAORIS/USDT:USDT | 3.520398051612667 | 28.269986267732673 | 25.06314069231914 | -3.2068455754135305 |
| OG/USDT:USDT | 3.4539503907004514 | 25.65936989222686 | 22.452524316813328 | -3.2068455754135305 |
| VELVET/USDT:USDT | 3.4368324602446862 | 43.653656849604985 | 40.446811274191454 | -3.2068455754135305 |

## Next Research Action

Investigate repeated candidates first. If repeated candidates remain empty, loosen only selector diagnostics, not backtest math.
