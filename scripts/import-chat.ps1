param(
    [Parameter(Mandatory = $true)]
    [string]$Path,
    [string]$Slug,
    [double]$ChunkIntervalSec,
    [switch]$Recreate,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$resolved = (Resolve-Path -LiteralPath $Path).Path
$py = Join-Path $Root "services\import\import_telegram.py"
$pyArgs = @($py, "--file", $resolved)
if ($Slug) { $pyArgs += @("--slug", $Slug) }
if ($PSBoundParameters.ContainsKey("ChunkIntervalSec")) {
    $pyArgs += @("--chunk-interval-sec", "$ChunkIntervalSec")
}
if ($Recreate) { $pyArgs += "--recreate" }
if ($DryRun) { $pyArgs += "--dry-run" }

$env:OPENSEARCH_URL = "https://127.0.0.1:9200"
python @pyArgs
