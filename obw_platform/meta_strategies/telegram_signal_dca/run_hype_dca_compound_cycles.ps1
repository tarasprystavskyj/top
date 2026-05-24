param(
    [int]$Cycles = 3,
    [double]$InitialEquity = 500.0,
    [double]$MaxTargetNotional = 500.0,
    [double]$MaxMtmDdPct = 50.0,
    [double]$MinTradeMtmPct = -50.0,
    [int]$RandomCandidates = 0,
    [string]$CandidateFilter = "t500_b16_s0p25-0p35-0p55_w0p8-1p2-2p2"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$Script = Join-Path $Root "obw_platform\meta_strategies\telegram_signal_dca\run_hype_dca_parameter_search.py"
$ReportRoot = Join-Path $Root "obw_platform\meta_strategies\telegram_signal_dca\reports\binance_430051_hype_v21_loop_20260523"
$LogDir = Join-Path $ReportRoot "compound_cycle_logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

for ($i = 1; $i -le $Cycles; $i++) {
    $seed = 3000 + $i
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $out = Join-Path $ReportRoot ("dca_grounded_compound_cycle_{0:D3}_{1}" -f $i, $stamp)

    python $Script `
        --out-dir $out `
        --initial-equity $InitialEquity `
        --target-scale 1 `
        --max-target-notional $MaxTargetNotional `
        --position-sizing-mode compound `
        --fill-mode close_beyond_skip_boundary `
        --strict-fill-mode close_beyond_skip_boundary `
        --candidate-filter $CandidateFilter `
        --max-mtm-dd-pct $MaxMtmDdPct `
        --min-trade-mtm-pct $MinTradeMtmPct `
        --random-candidates $RandomCandidates `
        --seed $seed `
        --topn 30 *> (Join-Path $LogDir ("compound_cycle_{0:D3}.log" -f $i))
}
