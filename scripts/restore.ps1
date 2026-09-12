[CmdletBinding(SupportsShouldProcess, ConfirmImpact = "High")]
param(
    [Parameter(Mandatory)][string]$BackupPath,
    [string]$ProjectName = "onec-kb",
    [switch]$RestoreConfig,
    [switch]$Start
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $PSScriptRoot
$BackupPath = [System.IO.Path]::GetFullPath($BackupPath)
$manifestPath = Join-Path $BackupPath "manifest.json"
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw "Backup manifest not found: $manifestPath"
}
$manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
if ($manifest.format_version -ne 1) {
    throw "Unsupported backup format version: $($manifest.format_version)"
}

foreach ($file in $manifest.files) {
    $path = Join-Path $BackupPath ([string]$file.path).Replace("/", "\")
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Backup file is missing: $($file.path)"
    }
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant()
    if ($actual -ne $file.sha256) {
        throw "Checksum mismatch for $($file.path): expected $($file.sha256), found $actual."
    }
}
Write-Host "Verified $($manifest.files.Count) backup file checksums."

if (-not $PSCmdlet.ShouldProcess(
    "Docker Compose project '$ProjectName'",
    "replace MongoDB, Neo4j, Weaviate, and Redis volumes from '$BackupPath'"
)) {
    return
}

$volumeMap = [ordered]@{
    mongodb = "mongo_data"
    neo4j = "neo4j_data"
    weaviate = "weaviate_data"
    redis = "redis_data"
}
$helperImage = [string]$manifest.helper_image
if (-not $helperImage) {
    $helperImage = "alpine:3.21@sha256:48b0309ca019d89d40f670aa1bc06e426dc0931948452e8491e3d65087abc07d"
}

Push-Location $Root
try {
    & docker compose -p $ProjectName stop --timeout 60 atlas web caddy mongodb neo4j weaviate redis
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to stop target project before restore."
    }

    foreach ($entry in $volumeMap.GetEnumerator()) {
        $volumeName = "$ProjectName`_$($entry.Value)"
        $archive = Join-Path $BackupPath "data\$($entry.Key).tar.gz"
        if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) {
            throw "Required archive is missing: $archive"
        }
        & docker volume create $volumeName *> $null
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to create target volume $volumeName."
        }
        Write-Host "Restoring $($entry.Key) into $volumeName..."
        & docker run --rm `
            --mount "type=volume,source=$volumeName,target=/target" `
            --mount "type=bind,source=$BackupPath,target=/backup,readonly" `
            $helperImage sh -c "find /target -mindepth 1 -delete && tar xzf /backup/data/$($entry.Key).tar.gz -C /target"
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to restore $volumeName."
        }
    }

    if ($RestoreConfig) {
        $configRoot = Join-Path $BackupPath "config"
        foreach ($item in Get-ChildItem -LiteralPath $configRoot -Force) {
            Copy-Item -LiteralPath $item.FullName -Destination $Root -Recurse -Force
        }
        Write-Host "Configuration files restored (including the backed-up local .env)."
    }

    if ($Start) {
        & docker compose -p $ProjectName up -d
        if ($LASTEXITCODE -ne 0) {
            throw "Data restored, but the target project did not start successfully."
        }
    }
    Write-Host "Restore completed for Compose project '$ProjectName'."
}
finally {
    Pop-Location
}
