[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

function Get-Sha256([string]$Path) {
    $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return -join ($sha.ComputeHash($stream) | ForEach-Object { $_.ToString('X2') })
    } finally {
        $sha.Dispose()
        $stream.Dispose()
    }
}

function Resolve-RelativePath([string]$Root, [string]$RelativePath) {
    return [IO.Path]::GetFullPath((Join-Path $Root $RelativePath.Replace('/', [IO.Path]::DirectorySeparatorChar)))
}

try {
    $projectRoot = $PSScriptRoot
    $gameRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot '..'))
    $baseManifest = Get-Content -Raw -LiteralPath (Join-Path $projectRoot 'manifest.json') | ConvertFrom-Json
    $gameExe = Resolve-RelativePath $gameRoot $baseManifest.supported_game.executable.path
    if (-not (Test-Path -LiteralPath $gameExe -PathType Leaf) -or
        (Get-Sha256 $gameExe) -ne $baseManifest.supported_game.executable.sha256.ToUpperInvariant()) {
        throw 'Place this project directly inside its supported Danganronpa V3 game directory.'
    }
    if (Get-Process -Name 'Dangan3Win' -ErrorAction SilentlyContinue) {
        throw 'Danganronpa V3 is running. Close the game before uninstalling.'
    }

    $python = (Get-Command python -ErrorAction Stop).Source
    $modTool = Join-Path $projectRoot 'tools\modtool.py'
    $assetRoot = Join-Path $projectRoot 'assets'
    $localRoot = Join-Path $gameRoot 'DualSense_UI_Mod_Data'
    $originalEntryRoot = Join-Path $localRoot 'original_entries'
    $statePath = Join-Path $localRoot 'installed-state.json'
    if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
        throw 'No installation record exists. Use Steam Verify if game files need restoration.'
    }
    $state = Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json
    if (-not $state.installed) {
        Write-Host 'The mod is already marked as uninstalled.'
        exit 0
    }
    if ([string]::IsNullOrWhiteSpace($state.runtime_manifest)) {
        throw 'This is a legacy installation record. Run the installer once for the active language or use Steam Verify.'
    }
    $manifestPath = Resolve-RelativePath $localRoot $state.runtime_manifest
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw 'The saved runtime manifest is missing. Use Steam Verify to restore the game.'
    }
    $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json

    Write-Host "Classifying installed archives for language $($state.language_code)..."
    $classifyOutput = & $python $modTool classify --manifest $manifestPath --game-root $gameRoot --state $statePath --entries-only
    if ($LASTEXITCODE -ne 0) {
        throw 'An archive changed after installation. Use Steam Verify to restore the game.'
    }
    $classification = ($classifyOutput -join [Environment]::NewLine) | ConvertFrom-Json
    $hasPatched = [bool]$classification.any_patched

    if ($hasPatched) {
        & $python $modTool verify-originals --manifest $manifestPath --original-entry-dir $originalEntryRoot
        if ($LASTEXITCODE -ne 0) {
            throw 'Compact rollback data is missing or invalid. Use Steam Verify to restore the game.'
        }
        $variantKey = if ($state.variant -eq 'DualSense') { 'dualsense' } else { 'dualshock4' }
        $buildRoot = Join-Path $localRoot ("build\$($state.language_code)\" + $variantKey)
        & $python $modTool build --manifest $manifestPath --original-entry-dir $originalEntryRoot --asset-dir $assetRoot --variant $variantKey --output-dir $buildRoot
        if ($LASTEXITCODE -ne 0) { throw 'Could not rebuild the installed payload for safe restoration.' }

        Write-Host 'Restoring only the modified CPK entries...'
        & $python $modTool restore --manifest $manifestPath --game-root $gameRoot --payload-dir $buildRoot --original-entry-dir $originalEntryRoot
        if ($LASTEXITCODE -ne 0) {
            throw 'Compact restoration failed. Use Steam Verify to restore the game.'
        }
    } else {
        Write-Host 'All currently installed supported archives are already original.'
    }

    $state.installed = $false
    $state | Add-Member -NotePropertyName uninstalled_utc -NotePropertyValue ((Get-Date).ToUniversalTime().ToString('o')) -Force
    $state | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $statePath -Encoding UTF8
    Write-Host 'Uninstall completed successfully. Compact original entries were retained.'
    exit 0
} catch {
    Write-Error $_.Exception.Message
    exit 1
}
