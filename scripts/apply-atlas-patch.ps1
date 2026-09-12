[CmdletBinding()]
param(
    [string]$AtlasDirectory,
    [string]$ManifestPath
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $PSScriptRoot
if (-not $AtlasDirectory) {
    $AtlasDirectory = Join-Path $Root "vendor\beever-atlas"
}
if (-not $ManifestPath) {
    $ManifestPath = Join-Path $Root "patches\beever-atlas-v0.2.0-onec-kb.manifest.json"
}

$AtlasDirectory = [System.IO.Path]::GetFullPath($AtlasDirectory)
$ManifestPath = [System.IO.Path]::GetFullPath($ManifestPath)
if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
    throw "Atlas patch manifest not found: $ManifestPath"
}
if (-not (Test-Path -LiteralPath (Join-Path $AtlasDirectory ".git"))) {
    throw "Atlas checkout is not a Git worktree: $AtlasDirectory"
}

$manifest = Get-Content -Raw -LiteralPath $ManifestPath | ConvertFrom-Json
$patchPath = Join-Path (Split-Path -Parent $ManifestPath) $manifest.patch_file
if (-not (Test-Path -LiteralPath $patchPath -PathType Leaf)) {
    throw "Atlas patch file not found: $patchPath"
}

$actualPatchHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $patchPath).Hash.ToLowerInvariant()
if ($actualPatchHash -ne $manifest.patch_sha256) {
    throw "Atlas patch SHA256 mismatch: expected $($manifest.patch_sha256), found $actualPatchHash."
}

$head = (& git -C $AtlasDirectory rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $head -ne $manifest.base_commit) {
    throw "Atlas HEAD must be pinned base $($manifest.base_commit); found $head. Local work was not changed."
}

$tagObject = (& git -C $AtlasDirectory rev-parse "$($manifest.tag)^{tag}" 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $tagObject -ne $manifest.tag_object) {
    throw "Atlas tag object mismatch: expected $($manifest.tag_object), found $tagObject."
}

# Reverse-check is the idempotency probe: it only succeeds when every patch
# hunk and added file is already present. Its expected non-zero result on a
# clean checkout must not become a PowerShell NativeCommandError under Stop.
$previousErrorPreference = $ErrorActionPreference
$ErrorActionPreference = "SilentlyContinue"
& git -C $AtlasDirectory apply --reverse --check --whitespace=nowarn $patchPath 2>$null
$alreadyApplied = $LASTEXITCODE -eq 0
$ErrorActionPreference = $previousErrorPreference
if ($alreadyApplied) {
    $state = "already applied"
}
else {
    $statusBefore = (& git -C $AtlasDirectory status --porcelain=v1 --untracked-files=all | Out-String).Trim()
    if ($statusBefore) {
        throw "Atlas has local or partially-applied changes. Patch was not applied; local work was preserved."
    }

    & git -C $AtlasDirectory apply --check --whitespace=error-all $patchPath
    if ($LASTEXITCODE -ne 0) {
        throw "Atlas patch preflight failed; checkout was not changed."
    }
    # Intent-to-add makes newly introduced files visible to `git diff`, while
    # leaving the complete patchset uncommitted and reviewable in the checkout.
    & git -C $AtlasDirectory apply --intent-to-add --whitespace=nowarn $patchPath
    if ($LASTEXITCODE -ne 0) {
        throw "Atlas patch application failed."
    }
    $state = "applied"
}

& git -C $AtlasDirectory apply --reverse --check --whitespace=nowarn $patchPath
if ($LASTEXITCODE -ne 0) {
    throw "Atlas patch verification failed after application."
}
& git -C $AtlasDirectory diff --check HEAD
if ($LASTEXITCODE -ne 0) {
    throw "Atlas patched worktree failed git diff --check."
}
foreach ($relativePath in $manifest.expected_paths) {
    $expectedPath = Join-Path $AtlasDirectory ([string]$relativePath)
    if (-not (Test-Path -LiteralPath $expectedPath -PathType Leaf)) {
        throw "Atlas patched worktree is missing expected path: $relativePath"
    }
}

Write-Host "Atlas patchset $state and verified (SHA256 $actualPatchHash)."
