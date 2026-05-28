param(
    [switch]$DryRun,
    [switch]$SkipCollect,
    [switch]$SkipRank,
    [switch]$InvokeCodexResume,
    [string]$CodexResumeId = "019e278d-e0a2-7430-8dde-fdc029a7802f",
    [string]$CodexExe = "codex",
    [string]$PythonExe = "python",
    [string]$Exchange = "bybit",
    [string]$UniverseFile = "obw_platform/universe/universe_ena_second_leg_candidates.txt",
    [string]$EnaNpz = "DB/ena_ohlcv_30s_1y_from_ticks_compat_np1.npz",
    [string]$DbOut = "DB/combined_cache_30s_ena_second_leg_candidates_1y.db",
    [string]$NpzOut = "DB/ohlcv_30s_ena_second_leg_candidates_1y.npz",
    [string]$CandidateGlob = "DB/*30s*1y*.npz",
    [string]$ReportDir = "docs/ena_second_leg_data/reports",
    [string]$StartUtc = "2025-03-01 00:00:00",
    [string]$EndUtc = "2026-03-02 00:00:00"
)

$ErrorActionPreference = "Stop"

function RepoRoot {
    $scriptDir = Split-Path -Parent $PSCommandPath
    return (Resolve-Path (Join-Path $scriptDir "..\..")).Path
}

function Show-Command {
    param([string[]]$CommandArgs)
    Write-Host ("[cmd] " + ($CommandArgs -join " "))
}

function Invoke-Checked {
    param([string[]]$CommandArgs)
    Show-Command $CommandArgs
    if ($DryRun) {
        return
    }
    & $CommandArgs[0] @($CommandArgs | Select-Object -Skip 1)
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE"
    }
}

$root = RepoRoot
Set-Location $root

Write-Host "[cfg] repo=$root"
Write-Host "[cfg] dry_run=$DryRun skip_collect=$SkipCollect skip_rank=$SkipRank invoke_codex_resume=$InvokeCodexResume"

$collectArgs = @(
    $PythonExe,
    "obw_platform/fetch_build_cache_and_fast_v1.py",
    "-i", $UniverseFile,
    "-t", "30s",
    "--start", $StartUtc,
    "--end", $EndUtc,
    "--exchange", $Exchange,
    "--ccxt-symbol-format", "usdtm",
    "--source", "trades_api",
    "--db-out", $DbOut,
    "--npz-out", $NpzOut,
    "--feature-set", "full",
    "--fresh",
    "--debug"
)

$rankArgs = @(
    $PythonExe,
    "obw_platform/tools/rank_ena_second_leg_npz.py",
    "--ena-npz", $EnaNpz,
    "--candidate-glob", $CandidateGlob,
    "--out-dir", $ReportDir
)

if ($SkipCollect) {
    Write-Host "[skip] collection"
} else {
    Write-Host "[step] collection"
    Invoke-Checked $collectArgs
}

if ($SkipRank) {
    Write-Host "[skip] ranking"
} else {
    Write-Host "[step] ranking"
    Invoke-Checked $rankArgs
}

$reportDirResolved = (Resolve-Path $ReportDir -ErrorAction SilentlyContinue)
if ($null -eq $reportDirResolved) {
    New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null
    $reportDirResolved = Resolve-Path $ReportDir
}

$promptPath = Join-Path $reportDirResolved.Path "codex_resume_prompt.md"
$rankMd = Join-Path $ReportDir "ena_second_leg_rank.md"
$rankCsv = Join-Path $ReportDir "ena_second_leg_rank.csv"
$manifest = Join-Path $ReportDir "ena_second_leg_rank_manifest.json"

$prompt = @"
ENA second-leg 30s collection/ranking workflow is ready for review.

Repo: $root
Branch: demoquantfut-ena-second-leg-data

Inputs:
- ENA NPZ: $EnaNpz
- Candidate NPZ glob: $CandidateGlob
- Candidate universe: $UniverseFile

Outputs:
- Ranking markdown: $rankMd
- Ranking CSV: $rankCsv
- Ranking manifest: $manifest

Please inspect the reports, identify the best practical second leg for ENA stat-arb, and update the runbook/recommendation if the collected data is sufficient. If ranked_candidates is zero, explain which candidate data is still missing and the next safe collection command.
"@

$prompt | Set-Content -Path $promptPath -Encoding UTF8
Write-Host "[notify] wrote resume prompt: $promptPath"

if ($CodexResumeId) {
    $display = "$CodexExe resume $CodexResumeId `"<contents of $promptPath>`""
    Write-Host "[notify] codex resume command:"
    Write-Host "  $display"
    if ($InvokeCodexResume) {
        if ($DryRun) {
            Write-Host "[dry-run] would invoke codex resume"
        } else {
            Write-Host "[step] invoking codex resume"
            & $CodexExe resume $CodexResumeId $prompt
            if ($LASTEXITCODE -ne 0) {
                throw "codex resume failed with exit code $LASTEXITCODE"
            }
        }
    }
} else {
    Write-Host "[notify] pass -CodexResumeId <SESSION_ID> to override the default codex resume target"
}

Write-Host "[done] workflow wrapper complete"
