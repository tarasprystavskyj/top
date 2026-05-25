param(
    [double]$NotionalUsdt = 100.0,
    [double]$IntervalSec = 60.0
)

$ErrorActionPreference = "Stop"

$Root = "C:\python_scripts\top_1_dev_lifefix_push"
$ReportDir = Join-Path $Root "obw_platform\meta_strategies\telegram_signal_dca\reports\binance_copy_4728671486012660992_paper_20260525"
New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null

Push-Location $Root
try {
    python obw_platform\meta_strategies\telegram_signal_dca\paper_live_binance_copy_public_positions.py `
        --portfolio-id 4728671486012660992 `
        --state-path (Join-Path $ReportDir "paper_live_state.json") `
        --notional-usdt $NotionalUsdt `
        --history-page-size 20 `
        --timeout-sec 20 `
        --interval-sec $IntervalSec `
        --loop
} finally {
    Pop-Location
}
