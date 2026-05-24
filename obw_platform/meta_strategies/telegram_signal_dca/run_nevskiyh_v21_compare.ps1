param(
  [string]$SignalsCsv = "telegram_standard_bt_bundle\runs\kanalbacktest1_20260519_130722\nevskiyh_deep_valid_replay_signals.csv",
  [string]$PriceDb = "telegram_standard_bt_bundle\runs\kanalbacktest1_20260519_130722\nevskiyh_deep_signal_windows_3m_72h_bingx.db",
  [string]$V21Config = "obw_platform\configs\V21_strict_trend_stable_live_static9p38.yaml",
  [string]$OutDir = "obw_platform\meta_strategies\telegram_signal_dca\reports\nevskiyh_v21",
  [string]$DcaCounts = "0,1,2,3,4,5"
)

python obw_platform\meta_strategies\telegram_signal_dca\telegram_signal_dca_compare.py `
  --signals-csv $SignalsCsv `
  --price-db $PriceDb `
  --v21-config $V21Config `
  --out-dir $OutDir `
  --dca-counts $DcaCounts
