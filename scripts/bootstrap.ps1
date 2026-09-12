[CmdletBinding()]
param(
    [switch]$SkipPortCheck
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $PSScriptRoot
$AtlasDirectory = Join-Path $Root "vendor\beever-atlas"
$AtlasTag = "v0.2.0"
$AtlasTagObject = "1955037463cdccadf1761cf5bfe9b74e3f566d92"
$AtlasCommit = "3c0b35752d37c7b0b89c6a967462f1df5d71a608"
$AtlasRepository = "https://github.com/Beever-AI/beever-atlas.git"
$AtlasPatchInstaller = Join-Path $PSScriptRoot "apply-atlas-patch.ps1"
$RecommendedDockerBytes = 48GB
$MinimumFreeDiskBytes = 25GB

function Test-Command {
    param([Parameter(Mandatory)][string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function New-HexSecret {
    param([int]$ByteCount = 32)
    $bytes = New-Object byte[] $ByteCount
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    }
    finally {
        $rng.Dispose()
    }
    return ([System.BitConverter]::ToString($bytes)).Replace("-", "").ToLowerInvariant()
}

function Set-EnvValue {
    param(
        [Parameter(Mandatory)][string]$Content,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Value
    )
    $escaped = [regex]::Escape($Name)
    return [regex]::Replace($Content, "(?m)^${escaped}=.*$", "$Name=$Value")
}

Write-Host "Checking Windows, WSL2, Docker, GPU, memory, disk, and ports..."
if (-not [Environment]::Is64BitOperatingSystem) {
    throw "A 64-bit Windows installation is required."
}
foreach ($command in @("git", "docker", "wsl.exe")) {
    if (-not (Test-Command $command)) {
        throw "Required command '$command' was not found in PATH."
    }
}

& docker info *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Docker Desktop is not running or its Linux engine is unavailable."
}
& docker compose version *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Docker Compose v2 is unavailable."
}

$wslList = ((& wsl.exe --list --verbose 2>&1 | Out-String) -replace "`0", "")
if ($LASTEXITCODE -ne 0 -or $wslList -notmatch "(?m)\s2\s*$") {
    throw "No WSL2 distribution was detected. Docker Desktop must use the WSL2 backend."
}

$dockerBytesText = (& docker info --format "{{.MemTotal}}" 2>$null | Out-String).Trim()
$dockerBytes = 0L
if ([long]::TryParse($dockerBytesText, [ref]$dockerBytes)) {
    $dockerGiB = [math]::Round($dockerBytes / 1GB, 1)
    if ($dockerBytes -lt $RecommendedDockerBytes) {
        Write-Warning "Docker currently exposes $dockerGiB GiB. Keep 32 GiB for setup only; configure 48 GiB before ingestion. This script does not modify global WSL/Docker settings."
    }
    else {
        Write-Host "Docker memory: $dockerGiB GiB."
    }
}

$drive = Get-PSDrive -Name ([System.IO.Path]::GetPathRoot($Root).Substring(0, 1))
if ($drive.Free -lt $MinimumFreeDiskBytes) {
    throw "At least 25 GiB of free disk space is required for images and the selected models."
}
Write-Host ("Free disk: {0:N1} GiB." -f ($drive.Free / 1GB))

if (Test-Command "nvidia-smi.exe") {
    $gpu = (& nvidia-smi.exe --query-gpu=name,memory.total,driver_version --format=csv,noheader 2>$null | Select-Object -First 1)
    if ($LASTEXITCODE -eq 0) {
        Write-Host "GPU: $gpu"
    }
}
else {
    Write-Warning "nvidia-smi.exe was not found. Install a WSL-compatible NVIDIA driver before starting llama.cpp."
}

$dockerRuntimes = (& docker info --format "{{json .Runtimes}}" 2>$null | Out-String)
if ($dockerRuntimes -notmatch "nvidia") {
    Write-Warning "Docker did not report an NVIDIA runtime. Verify GPU integration with Docker Desktop before starting the stack."
}

if (-not $SkipPortCheck) {
    foreach ($port in @(80, 443)) {
        $listener = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
        if ($listener) {
            throw "TCP port $port is already in use. Free it or adjust the Caddy port mapping."
        }
    }
}

New-Item -ItemType Directory -Force -Path (Join-Path $Root "vendor") | Out-Null
if (-not (Test-Path $AtlasDirectory)) {
    Write-Host "Creating pinned Beever Atlas $AtlasTag checkout..."
    & git clone --depth 1 --branch $AtlasTag $AtlasRepository $AtlasDirectory
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to clone Beever Atlas."
    }
    & git -C $AtlasDirectory switch -c "onec-kb-v0.2.0"
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to create the local Atlas patch branch."
    }
}

$head = (& git -C $AtlasDirectory rev-parse HEAD).Trim()
& git -C $AtlasDirectory merge-base --is-ancestor $AtlasCommit HEAD
if ($LASTEXITCODE -ne 0) {
    throw "vendor/beever-atlas is not based on pinned Atlas commit $AtlasCommit (found $head)."
}
$tagObject = (& git -C $AtlasDirectory rev-parse "$AtlasTag^{tag}" 2>$null | Out-String).Trim()
if ($tagObject -ne $AtlasTagObject) {
    throw "Atlas tag object mismatch: expected $AtlasTagObject, found $tagObject."
}
Write-Host "Atlas base verified: $AtlasTag -> $AtlasCommit (current HEAD $head)."

& $AtlasPatchInstaller -AtlasDirectory $AtlasDirectory
if ($LASTEXITCODE -ne 0) {
    throw "Unable to apply or verify the tracked Atlas patchset."
}

$atlasChanges = (& git -C $AtlasDirectory status --short | Out-String).Trim()
if ($atlasChanges) {
    Write-Host "Atlas tracked patchset is present and remains reviewable as local changes."
}

foreach ($directory in @(
    "models",
    "data\weaviate",
    "data\neo4j",
    "data\mongo",
    "data\redis"
)) {
    New-Item -ItemType Directory -Force -Path (Join-Path $Root $directory) | Out-Null
}

$envPath = Join-Path $Root ".env"
if (-not (Test-Path $envPath)) {
    $content = Get-Content -Raw -Path (Join-Path $Root ".env.example")
    $apiKey = New-HexSecret
    $adminToken = New-HexSecret
    $values = [ordered]@{
        BEEVER_API_KEYS = $apiKey
        BEEVER_ADMIN_TOKEN = $adminToken
        VITE_BEEVER_API_KEY = $apiKey
        VITE_BEEVER_ADMIN_TOKEN = $adminToken
        BEEVER_MCP_API_KEYS = (New-HexSecret)
        BRIDGE_API_KEY = (New-HexSecret)
        LOADER_TOKEN_SECRET = (New-HexSecret)
        CREDENTIAL_MASTER_KEY = (New-HexSecret)
        WEAVIATE_API_KEY = (New-HexSecret)
        NEO4J_PASSWORD = (New-HexSecret)
        NEBULA_PASSWORD = (New-HexSecret)
    }
    foreach ($entry in $values.GetEnumerator()) {
        $content = Set-EnvValue -Content $content -Name $entry.Key -Value $entry.Value
    }
    [System.IO.File]::WriteAllText($envPath, $content, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "Created .env with cryptographically random, distinct secrets."
}
else {
    Write-Host "Existing .env preserved."
}

Write-Host ""
Write-Host "Bootstrap complete. Models were not downloaded and the stack was not started."
Write-Host "Download later: .\scripts\download-models.ps1 -Model All"
Write-Host "Validate config: docker compose config --quiet"
Write-Host "Start later: docker compose up -d"
Write-Host "After Caddy starts, explicitly trust its local CA only if desired:"
Write-Host "  docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt .\caddy-local-root.crt"
Write-Host "  Import-Certificate .\caddy-local-root.crt -CertStoreLocation Cert:\CurrentUser\Root"
