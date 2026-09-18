param(
    [Parameter(Mandatory = $true)]
    [string]$Path,
    [Parameter(Mandatory = $true)]
    [string]$SnapshotDate,
    [string]$Slug,
    [string]$IndexName,
    [switch]$Recreate,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if ($SnapshotDate -notmatch '^\d{4}-\d{2}-\d{2}$') {
    throw "SnapshotDate must be YYYY-MM-DD, for example 2025-07-18"
}

$resolved = (Resolve-Path -LiteralPath $Path).Path
$py = Join-Path $Root "services\import\import_telegram.py"
$pyArgs = @($py, "--file", $resolved, "--snapshot-date", $SnapshotDate)
if ($Slug) { $pyArgs += @("--slug", $Slug) }
if ($IndexName) { $pyArgs += @("--index-name", $IndexName) }
if ($Recreate) { $pyArgs += "--recreate" }
if ($DryRun) { $pyArgs += "--dry-run" }

$env:OPENSEARCH_URL = "https://127.0.0.1:9200"
python @pyArgs
