[CmdletBinding()]
param(
    [string]$BaseUrl = "https://atlas.localhost",
    [string]$EnvFile,
    [switch]$SkipAi
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $PSScriptRoot
if (-not $EnvFile) {
    $EnvFile = Join-Path $Root ".env"
}

function Get-EnvValue {
    param([Parameter(Mandatory)][string]$Name)
    $line = Get-Content -LiteralPath $EnvFile |
        Where-Object { $_ -match "^$([regex]::Escape($Name))=" } |
        Select-Object -First 1
    if (-not $line) {
        throw "Missing $Name in $EnvFile."
    }
    return ($line -split "=", 2)[1].Trim()
}

function Invoke-AtlasJson {
    param(
        [Parameter(Mandatory)][string]$Path,
        [ValidateSet("GET", "POST")][string]$Method = "GET",
        [object]$Body,
        [switch]$Anonymous
    )
    $outputFile = [System.IO.Path]::GetTempFileName()
    try {
        $uri = "$($BaseUrl.TrimEnd('/'))$Path"
        $arguments = @("-sS", "-k", "-o", $outputFile, "-w", "%{http_code}")
        if (-not $Anonymous) {
            $arguments += @("-H", "Authorization: Bearer $script:ApiKey")
        }
        if ($Method -eq "POST") {
            $json = $Body | ConvertTo-Json -Depth 10 -Compress
            $arguments += @(
                "-X", "POST",
                "-H", "Content-Type: application/json",
                "--data-binary", $json
            )
        }
        $arguments += $uri
        $statusCode = & curl.exe @arguments
        if ($LASTEXITCODE -ne 0) {
            throw "curl failed for $uri."
        }
        $payload = Get-Content -Raw -LiteralPath $outputFile
        return [pscustomobject]@{
            StatusCode = [int]$statusCode
            Body = $payload
            Json = if ($payload) { $payload | ConvertFrom-Json } else { $null }
        }
    }
    finally {
        Remove-Item -LiteralPath $outputFile -Force -ErrorAction SilentlyContinue
    }
}

Push-Location $Root
try {
    & docker compose config --quiet
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose config failed."
    }

    $rows = @(& docker compose ps --format json | ForEach-Object { $_ | ConvertFrom-Json })
    $required = @("atlas", "web", "weaviate", "neo4j", "mongodb", "redis", "llama-chat", "llama-embedding", "caddy")
    foreach ($service in $required) {
        $row = $rows | Where-Object Service -eq $service | Select-Object -First 1
        if (-not $row -or $row.State -ne "running") {
            throw "Service '$service' is not running."
        }
        if ($row.Health -and $row.Health -ne "healthy") {
            throw "Service '$service' health is '$($row.Health)'."
        }
    }

    foreach ($row in $rows) {
        foreach ($publisher in @($row.Publishers)) {
            if ($publisher.PublishedPort -and (
                $row.Service -ne "caddy" -or
                $publisher.URL -notin @("127.0.0.1", "::1")
            )) {
                throw "Unexpected published port on $($row.Service): $($publisher.URL):$($publisher.PublishedPort)."
            }
        }
    }

    $script:ApiKey = ((Get-EnvValue "BEEVER_API_KEYS") -split ",")[0].Trim()
    $health = Invoke-AtlasJson -Path "/api/health" -Anonymous
    if ($health.StatusCode -ne 200 -or $health.Json.status -ne "healthy") {
        throw "Deep health check failed: HTTP $($health.StatusCode) $($health.Body)"
    }

    $unauthorized = Invoke-AtlasJson -Path "/api/channels" -Anonymous
    if ($unauthorized.StatusCode -ne 401) {
        throw "Authenticated endpoint accepted an anonymous request (HTTP $($unauthorized.StatusCode))."
    }

    $channels = Invoke-AtlasJson -Path "/api/channels"
    if ($channels.StatusCode -ne 200) {
        throw "Channel listing failed: HTTP $($channels.StatusCode)."
    }

    if (-not $SkipAi) {
        $aiProbe = @'
import httpx
chat = httpx.get("http://llama-chat:8080/v1/models", timeout=30)
chat.raise_for_status()
emb = httpx.post(
    "http://llama-embedding:8080/v1/embeddings",
    json={"model": "bge-m3", "input": ["smoke test"]},
    timeout=60,
)
emb.raise_for_status()
dimension = len(emb.json()["data"][0]["embedding"])
assert dimension == 1024, f"expected 1024 embedding dimensions, got {dimension}"
print("AI endpoints healthy; embedding dimensions=1024")
'@
        $encodedProbe = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($aiProbe))
        & docker compose exec -T atlas python -c "import base64;exec(base64.b64decode('$encodedProbe'))"
        if ($LASTEXITCODE -ne 0) {
            throw "Local AI endpoint smoke check failed."
        }
    }

    Write-Host "Smoke test passed: compose, private ports, deep health, auth, API, and local AI."
}
finally {
    Pop-Location
}
