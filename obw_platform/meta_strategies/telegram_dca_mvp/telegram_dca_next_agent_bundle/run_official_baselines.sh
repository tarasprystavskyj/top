#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/local_test_bundle"

python obw_platform/meta_strategies/telegram_dca_mvp/run_telegram_simple_baseline_npz.py \
  --npz DB/telegram_signals_1m_event_windows_bingx.npz \
  --signals-csv DB/telegram_signal_standard_bt/telegram_signals_extracted.csv \
  --events DB/telegram_signal_standard_bt/telegram_channel_exit_events.csv \
  --out-dir ../reports/rerun_simple_all_tp2_50 \
  --entry-mode close_in_zone \
  --signal-ttl-hours 72 \
  --signal-hard-ttl-sec 3600 \
  --exit-at-tp 2 \
  --tp-margin-weights 0.5,0.5,0

python obw_platform/meta_strategies/telegram_dca_mvp/run_telegram_simple_baseline_npz.py \
  --npz DB/telegram_signals_1m_event_windows_bingx.npz \
  --signals-csv DB/telegram_signal_standard_bt/telegram_signals_extracted.csv \
  --events DB/telegram_signal_standard_bt/telegram_channel_exit_events.csv \
  --out-dir ../reports/rerun_simple_all_fixed_thirds \
  --entry-mode close_in_zone \
  --signal-ttl-hours 72 \
  --signal-hard-ttl-sec 3600 \
  --exit-at-tp 3 \
  --tp-margin-weights 0.333333,0.333333,0.333334
