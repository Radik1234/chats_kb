param(
    [switch]$SkipUp
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$envFile = Join-Path $Root ".env"
$example = Join-Path $Root ".env.example"
if (-not (Test-Path $envFile)) {
    if (-not (Test-Path $example)) {
        throw ".env.example not found"
    }
    Copy-Item $example $envFile
    Write-Host "Created .env from .env.example"
}

try {
    wsl -d docker-desktop -- /sbin/sysctl -w vm.max_map_count=262144 | Out-Host
} catch {
    Write-Warning "Could not set vm.max_map_count. If OpenSearch fails, run: wsl -d docker-desktop -- /sbin/sysctl -w vm.max_map_count=262144"
}

if (-not $SkipUp) {
    docker compose up -d
    docker compose ps
    Write-Host ""
    Write-Host "Dashboards: http://127.0.0.1:5601  (admin / password from .env)"
    Write-Host "OpenSearch: https://127.0.0.1:9200"
}
