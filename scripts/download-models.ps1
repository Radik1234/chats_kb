[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet("Llm", "Embedding", "All")]
    [string]$Model,

    [switch]$VerifyOnly
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $PSScriptRoot
$ModelDirectory = Join-Path $Root "models"
New-Item -ItemType Directory -Force -Path $ModelDirectory | Out-Null

# URLs include immutable Hugging Face repository revisions. SHA256 values are
# the repositories' Git LFS object IDs and are verified after every download.
$catalog = @(
    [pscustomobject]@{
        Kind = "Llm"
        Name = "Qwen2.5-14B-Instruct-Q4_K_M.gguf"
        Size = 8988110976L
        Sha256 = "e47ad95dad6ff848b431053b375adb5d39321290ea2c638682577dafca87c008"
        Url = "https://huggingface.co/bartowski/Qwen2.5-14B-Instruct-GGUF/resolve/05244aa5d871c661c80082a15d3bce44714d068d/Qwen2.5-14B-Instruct-Q4_K_M.gguf"
    },
    [pscustomobject]@{
        Kind = "Embedding"
        Name = "bge-m3-Q8_0.gguf"
        Size = 634553760L
        Sha256 = "950f4a8e5e19477a6d3c26d2f162233c20002c601f75e4b002e3239997821167"
        Url = "https://huggingface.co/gpustack/bge-m3-GGUF/resolve/2d48f1737679ad900d5c26c5aad5410e9c70fdca/bge-m3-Q8_0.gguf"
    }
)

if ($Model -ne "All") {
    $catalog = @($catalog | Where-Object Kind -eq $Model)
}

function Test-ModelHash {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Expected
    )
    if (-not (Test-Path $Path -PathType Leaf)) {
        return $false
    }
    $actual = (Get-FileHash -Algorithm SHA256 -Path $Path).Hash.ToLowerInvariant()
    return $actual -eq $Expected
}

if ($VerifyOnly) {
    $invalid = $false
    foreach ($item in $catalog) {
        $path = Join-Path $ModelDirectory $item.Name
        if (Test-ModelHash -Path $path -Expected $item.Sha256) {
            Write-Host "OK  $($item.Name)"
        }
        else {
            Write-Error "MISSING/INVALID  $($item.Name)"
            $invalid = $true
        }
    }
    if ($invalid) {
        exit 1
    }
    exit 0
}

if (-not (Get-Command curl.exe -ErrorAction SilentlyContinue)) {
    throw "curl.exe is required for resumable multi-gigabyte downloads."
}

$required = 0L
foreach ($item in $catalog) {
    $path = Join-Path $ModelDirectory $item.Name
    if (-not (Test-Path $path)) {
        $required += $item.Size
    }
}
$drive = Get-PSDrive -Name ([System.IO.Path]::GetPathRoot($ModelDirectory).Substring(0, 1))
if ($drive.Free -lt ($required + 2GB)) {
    throw ("Insufficient free space. Need approximately {0:N1} GiB plus 2 GiB reserve." -f ($required / 1GB))
}

Write-Host ("Selected download size: approximately {0:N1} GiB." -f ($required / 1GB))
foreach ($item in $catalog) {
    $destination = Join-Path $ModelDirectory $item.Name
    $partial = "$destination.partial"

    if (Test-ModelHash -Path $destination -Expected $item.Sha256) {
        Write-Host "Already verified: $($item.Name)"
        continue
    }
    if (Test-Path $destination) {
        throw "Existing file has the wrong SHA256: $destination. Remove it explicitly before retrying."
    }

    Write-Host "Downloading $($item.Name) (resumable)..."
    & curl.exe --location --fail --retry 5 --retry-delay 3 --continue-at - --ssl-no-revoke --output $partial $item.Url
    if ($LASTEXITCODE -ne 0) {
        throw "Download failed; resumable partial file was kept at $partial."
    }
    if (-not (Test-ModelHash -Path $partial -Expected $item.Sha256)) {
        throw "SHA256 verification failed for $partial. The partial file was preserved for inspection."
    }
    Move-Item -Force -Path $partial -Destination $destination
    Write-Host "Verified: $($item.Name)"
}
