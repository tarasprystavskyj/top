set -euo pipefail
cd /mnt/data/local_test_tar_bundle
NPZ=DB/telegram_signals_1m_event_windows_bingx.npz
SIG=DB/telegram_signal_standard_bt/telegram_signals_extracted.csv
EV=DB/telegram_signal_standard_bt/telegram_channel_exit_events.csv
BASE=obw_platform/meta_strategies/telegram_dca_mvp/reports/all_signals_tests
SIMPLE=obw_platform/meta_strategies/telegram_dca_mvp/run_telegram_simple_baseline_npz.py
DCA=obw_platform/meta_strategies/telegram_dca_mvp/run_telegram_dca_mvp_npz.py
COMMON="--npz $NPZ --signals-csv $SIG --entry-mode close_in_zone --signal-ttl-hours 72 --signal-hard-ttl-sec 3600 --load-only-signal-symbols"
# 1 fixed thirds with events
python $SIMPLE $COMMON --events $EV --out-dir $BASE/pure_fixed_thirds_events --exit-at-tp 3 --tp-margin-weights 1,1,1
# 2 TP2 50/50 with events
python $SIMPLE $COMMON --events $EV --out-dir $BASE/pure_tp2_50_events --exit-at-tp 2 --tp-margin-weights 1,1,0
# 3 no channel close sensitivity
python $SIMPLE $COMMON --out-dir $BASE/pure_fixed_thirds_no_events --exit-at-tp 3 --tp-margin-weights 1,1,1
python $SIMPLE $COMMON --out-dir $BASE/pure_tp2_50_no_events --exit-at-tp 2 --tp-margin-weights 1,1,0
# 4 touch-zone sensitivity, fixed thirds with events
python $SIMPLE --npz $NPZ --signals-csv $SIG --events $EV --out-dir $BASE/touch_fixed_thirds_events --entry-mode touch_zone --signal-ttl-hours 72 --signal-hard-ttl-sec 3600 --exit-at-tp 3 --tp-margin-weights 1,1,1 --load-only-signal-symbols
# 5 constrained meta-DCA, TP2 50/50, events
python $DCA $COMMON --events $EV --out-dir $BASE/dca_tp2_50_1add_1p5x_events --exit-at-tp 2 --tp-margin-weights 1,1,0 --ignore-lower-exits --initial-notional 100 --meta-dca-adds 1 --meta-dca-total-notional-mult 1.5 --curve-every 200
python $DCA $COMMON --events $EV --out-dir $BASE/dca_tp2_50_2add_2p0x_events --exit-at-tp 2 --tp-margin-weights 1,1,0 --ignore-lower-exits --initial-notional 100 --meta-dca-adds 2 --meta-dca-total-notional-mult 2.0 --curve-every 200
# DCA fixed thirds maybe
python $DCA $COMMON --events $EV --out-dir $BASE/dca_fixed_thirds_1add_1p5x_events --exit-at-tp 3 --tp-margin-weights 1,1,1 --ignore-lower-exits --initial-notional 100 --meta-dca-adds 1 --meta-dca-total-notional-mult 1.5 --curve-every 200
python $DCA $COMMON --events $EV --out-dir $BASE/dca_fixed_thirds_2add_2p0x_events --exit-at-tp 3 --tp-margin-weights 1,1,1 --ignore-lower-exits --initial-notional 100 --meta-dca-adds 2 --meta-dca-total-notional-mult 2.0 --curve-every 200
