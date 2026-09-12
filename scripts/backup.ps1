[CmdletBinding()]
param(
    [string]$Destination,
    [string]$ProjectName = "onec-kb",
    [string]$HelperImage = "alpine:3.21@sha256:48b0309ca019d89d40f670aa1bc06e426dc0931948452e8491e3d65087abc07d",
    [switch]$KeepStopped
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $PSScriptRoot
if (-not $Destination) {
    $Destination = Join-Path $Root ("backups\onec-kb-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
}
$Destination = [System.IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $Destination) {
    throw "Backup destination already exists: $Destination"
}

$volumeMap = [ordered]@{
    mongodb = "mongo_data"
    neo4j = "neo4j_data"
    weaviate = "weaviate_data"
    redis = "redis_data"
}
$quiescedServices = @("caddy", "web", "atlas", "mongodb", "neo4j", "weaviate", "redis")
$runningBefore = @()

Push-Location $Root
try {
    & docker compose -p $ProjectName config --quiet
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose config failed."
    }
    $runningBefore = @(& docker compose -p $ProjectName ps --services --filter status=running)
    $toStop = @($quiescedServices | Where-Object { $_ -in $runningBefore })

    New-Item -ItemType Directory -Path $Destination | Out-Null
    $dataDir = New-Item -ItemType Directory -Path (Join-Path $Destination "data")
    $configDir = New-Item -ItemType Directory -Path (Join-Path $Destination "config")

    if ($toStop.Count -gt 0) {
        Write-Host "Quiescing writers and data services: $($toStop -join ', ')"
        & docker compose -p $ProjectName stop --timeout 60 @toStop
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to quiesce the stack."
        }
    }

    foreach ($entry in $volumeMap.GetEnumerator()) {
        $volumeName = "$ProjectName`_$($entry.Value)"
        & docker volume inspect $volumeName *> $null
        if ($LASTEXITCODE -ne 0) {
            throw "Required Docker volume does not exist: $volumeName"
        }
        $archiveName = "$($entry.Key).tar.gz"
        Write-Host "Archiving $($entry.Key) ($volumeName)..."
        & docker run --rm `
            --mount "type=volume,source=$volumeName,target=/source,readonly" `
            --mount "type=bind,source=$($dataDir.FullName),target=/backup" `
            $HelperImage sh -c "cd /source && tar czf /backup/$archiveName ."
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to archive $volumeName."
        }
    }

    $configFiles = @(
        ".env",
        ".env.example",
        "docker-compose.yml",
        "atlas.yaml",
        "Caddyfile",
        ".gitignore"
    )
    foreach ($relative in $configFiles) {
        $source = Join-Path $Root $relative
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            Copy-Item -LiteralPath $source -Destination (Join-Path $configDir $relative)
        }
    }
    foreach ($directory in @("config", "patches")) {
        $source = Join-Path $Root $directory
        if (Test-Path -LiteralPath $source -PathType Container) {
            Copy-Item -LiteralPath $source -Destination (Join-Path $configDir $directory) -Recurse
        }
    }

    $files = @(
        Get-ChildItem -LiteralPath $Destination -File -Recurse |
            Where-Object Name -ne "manifest.json" |
            ForEach-Object {
                $relativePath = $_.FullName.Substring(
                    $Destination.TrimEnd("\").Length
                ).TrimStart("\").Replace("\", "/")
                [ordered]@{
                    path = $relativePath
                    bytes = $_.Length
                    sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash.ToLowerInvariant()
                }
            }
    )
    $manifest = [ordered]@{
        format_version = 1
        created_at = (Get-Date).ToUniversalTime().ToString("o")
        compose_project = $ProjectName
        consistency = "cold-volume-snapshot"
        helper_image = $HelperImage
        volumes = $volumeMap
        files = $files
    }
    $manifest | ConvertTo-Json -Depth 8 |
        Set-Content -LiteralPath (Join-Path $Destination "manifest.json") -Encoding utf8

    Write-Host "Backup created and checksummed: $Destination"
}
finally {
    if (-not $KeepStopped -and $runningBefore.Count -gt 0) {
        Write-Host "Restoring the previous running service set..."
        & docker compose -p $ProjectName up -d @runningBefore
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Backup completed, but one or more previously running services did not restart."
        }
    }
    Pop-Location
}
