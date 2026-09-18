$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Get-DotEnvValue([string]$Name) {
    $line = Get-Content (Join-Path $Root ".env") | Where-Object { $_ -match "^$Name=" }
    if (-not $line) { throw "$Name is missing in .env" }
    return ($line -split "=", 2)[1]
}

$password = Get-DotEnvValue "OPENSEARCH_INITIAL_ADMIN_PASSWORD"
$authArgs = @("-sk", "-u", "admin:$password")

Write-Host "Cluster health..."
$healthJson = curl.exe @authArgs "https://127.0.0.1:9200/_cluster/health"
if ($LASTEXITCODE -ne 0) { throw "OpenSearch is not reachable on https://127.0.0.1:9200" }
$health = $healthJson | ConvertFrom-Json
Write-Host ("  status={0} nodes={1}" -f $health.status, $health.number_of_nodes)
if ($health.status -notin @("green", "yellow")) {
    throw "OpenSearch cluster is $($health.status)"
}

Write-Host "Indices..."
curl.exe @authArgs "https://127.0.0.1:9200/_cat/indices?v&s=index"
if ($LASTEXITCODE -ne 0) { throw "Failed to list indices" }

Write-Host "Search check..."
python (Join-Path $Root "scripts\verify_search.py")
if ($LASTEXITCODE -ne 0) { throw "search check failed" }

Write-Host "MCP port..."
$tcp = Test-NetConnection -ComputerName 127.0.0.1 -Port 9900 -WarningAction SilentlyContinue
if (-not $tcp.TcpTestSucceeded) {
    Write-Warning "MCP is not listening on 127.0.0.1:9900 (optional profile mcp)"
}

Write-Host "Dashboards..."
curl.exe -sf "http://127.0.0.1:5601/app/login" | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Dashboards login page is not ready yet"
}

Write-Host "OK"
