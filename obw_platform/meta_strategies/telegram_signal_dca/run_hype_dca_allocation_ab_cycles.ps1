param(
    [ValidateSet("range_lookback", "golden_history")]
    [string]$AllocationMode = "range_lookback",
    [int]$Cycles = 6,
    [double]$InitialEquity = 500.0,
    [double]$LookbackDays = 7.0,
    [double]$MinTradeMtmPct = -50.0,
    [int]$RandomCandidates = 200
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$Script = Join-Path $Root "obw_platform\meta_strategies\telegram_signal_dca\run_hype_dca_allocation_ab.py"
$ReportRoot = Join-Path $Root "obw_platform\meta_strategies\telegram_signal_dca\reports\binance_430051_hype_v21_loop_20260523"
$LogDir = Join-Path $ReportRoot "allocation_ab_cycle_logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

for ($i = 1; $i -le $Cycles; $i++) {
    $seed = 4100 + $i
    if ($AllocationMode -eq "golden_history") {
        $seed = 5100 + $i
    }
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $out = Join-Path $ReportRoot ("dca_allocation_{0}_cycle_{1:D3}_{2}" -f $AllocationMode, $i, $stamp)

    python $Script `
        --out-dir $out `
        --initial-equity $InitialEquity `
        --lookback-days $LookbackDays `
        --min-trade-mtm-pct $MinTradeMtmPct `
        --fill-mode close_beyond_skip_boundary `
        --include-modes $AllocationMode `
        --random-candidates $RandomCandidates `
        --seed $seed `
        --topn 50 *> (Join-Path $LogDir ("{0}_cycle_{1:D3}.log" -f $AllocationMode, $i))
}
