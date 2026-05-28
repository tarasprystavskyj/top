param(
    [int]$Cycles = 3,
    [double]$InitialEquity = 500.0,
    [double]$MaxMtmDdPct = 50.0,
    [double]$MinTradeMtmPct = -50.0
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$Script = Join-Path $Root "obw_platform\meta_strategies\telegram_signal_dca\run_hype_dca_parameter_search.py"
$ReportRoot = Join-Path $Root "obw_platform\meta_strategies\telegram_signal_dca\reports\binance_430051_hype_v21_loop_20260523"
$LogDir = Join-Path $ReportRoot "cycle_logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

for ($i = 1; $i -le $Cycles; $i++) {
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $fixedOut = Join-Path $ReportRoot ("dca_cycles_ie500_{0:D3}_fixed_notional_{1}" -f $i, $stamp)
    $scaledOut = Join-Path $ReportRoot ("dca_cycles_ie500_{0:D3}_scaled_risk_{1}" -f $i, $stamp)
    $scaledSeed = 1000 + $i

    python $Script `
        --out-dir $fixedOut `
        --initial-equity $InitialEquity `
        --target-scale 1 `
        --random-candidates 300 `
        --seed $i `
        --max-mtm-dd-pct $MaxMtmDdPct `
        --min-trade-mtm-pct $MinTradeMtmPct `
        --topn 30 *> (Join-Path $LogDir ("cycle_{0:D3}_fixed.log" -f $i))

    python $Script `
        --out-dir $scaledOut `
        --initial-equity $InitialEquity `
        --target-scale ($InitialEquity / 100.0) `
        --random-candidates 300 `
        --seed $scaledSeed `
        --max-mtm-dd-pct $MaxMtmDdPct `
        --min-trade-mtm-pct $MinTradeMtmPct `
        --topn 30 *> (Join-Path $LogDir ("cycle_{0:D3}_scaled.log" -f $i))
}
