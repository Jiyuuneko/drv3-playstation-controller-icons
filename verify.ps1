[CmdletBinding()]
param(
    [switch]$Installed
)

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
    $baseManifestPath = Join-Path $projectRoot 'manifest.json'
    $baseManifest = Get-Content -Raw -LiteralPath $baseManifestPath | ConvertFrom-Json
    $gameExe = Resolve-RelativePath $gameRoot $baseManifest.supported_game.executable.path
    if ((Get-Sha256 $gameExe) -ne $baseManifest.supported_game.executable.sha256.ToUpperInvariant()) {
        throw 'Unsupported or changed game executable.'
    }
    foreach ($asset in $baseManifest.redistributable_assets) {
        $assetPath = Resolve-RelativePath $projectRoot $asset.path
        if (-not (Test-Path -LiteralPath $assetPath -PathType Leaf) -or
            ($asset.size -and (Get-Item -LiteralPath $assetPath).Length -ne [int64]$asset.size) -or
            (Get-Sha256 $assetPath) -ne $asset.sha256.ToUpperInvariant()) {
            throw "Project asset verification failed: $($asset.path)"
        }
    }
    Write-Host 'Redistributable project assets: OK'

    $localRoot = Join-Path $gameRoot 'DualSense_UI_Mod_Data'
    $originalEntryRoot = Join-Path $localRoot 'original_entries'
    $statePath = Join-Path $localRoot 'installed-state.json'
    $state = if (Test-Path -LiteralPath $statePath -PathType Leaf) {
        Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json
    } else { $null }

    if ($Installed) {
        if ($null -eq $state -or -not $state.installed) { throw 'No installed-state record exists.' }
        if ([string]::IsNullOrWhiteSpace($state.runtime_manifest)) {
            throw 'Legacy installation record detected. Run the selected installer to migrate the active language.'
        }
        $activeLanguage = if (Test-Path -LiteralPath (Join-Path $gameRoot 'language.txt')) {
            (Get-Content -Raw -LiteralPath (Join-Path $gameRoot 'language.txt')).Trim().ToUpperInvariant()
        } else { '' }
        if ($activeLanguage -ne $state.language_code) {
            throw "The game language changed from $($state.language_code) to $activeLanguage. Run the selected installer again to patch the active language controller graphics."
        }
        $manifestPath = Resolve-RelativePath $localRoot $state.runtime_manifest
        $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
        $python = (Get-Command python -ErrorAction Stop).Source
        $modTool = Join-Path $projectRoot 'tools\modtool.py'
        $assetRoot = Join-Path $projectRoot 'assets'
        $variantKey = if ($state.variant -eq 'DualSense') { 'dualsense' } else { 'dualshock4' }
        $buildRoot = Join-Path $localRoot ("build\$($state.language_code)\" + $variantKey)
        $classifyOutput = & $python $modTool classify --manifest $manifestPath --game-root $gameRoot --state $statePath
        if ($LASTEXITCODE -ne 0) { throw 'Installed archive classification failed.' }
        $classification = ($classifyOutput -join [Environment]::NewLine) | ConvertFrom-Json
        if (-not [bool]$classification.all_patched) { throw 'One or more installed archives are not patched.' }
        & $python $modTool verify-originals --manifest $manifestPath --original-entry-dir $originalEntryRoot
        if ($LASTEXITCODE -ne 0) { throw 'Compact original-entry verification failed.' }
        & $python $modTool build --manifest $manifestPath --original-entry-dir $originalEntryRoot --asset-dir $assetRoot --variant $variantKey --output-dir $buildRoot
        if ($LASTEXITCODE -ne 0) { throw 'Payload rebuild failed during verification.' }
        & $python $modTool verify --manifest $manifestPath --game-root $gameRoot --payload-dir $buildRoot --expect patched
        if ($LASTEXITCODE -ne 0) { throw 'Internal CPK entry verification failed.' }

        Write-Host "Installed $($state.variant) state for $($state.language_code) and compact rollback entries: OK"
    } else {
        $manifestPath = $baseManifestPath
        $manifest = $baseManifest
        if ($null -ne $state -and -not [string]::IsNullOrWhiteSpace($state.runtime_manifest)) {
            $savedManifestPath = Resolve-RelativePath $localRoot $state.runtime_manifest
            if (Test-Path -LiteralPath $savedManifestPath -PathType Leaf) {
                $manifestPath = $savedManifestPath
                $manifest = Get-Content -Raw -LiteralPath $savedManifestPath | ConvertFrom-Json
            }
        }
        $python = (Get-Command python -ErrorAction Stop).Source
        $modTool = Join-Path $projectRoot 'tools\modtool.py'
        $classifyArgs = @($modTool, 'classify', '--manifest', $manifestPath, '--game-root', $gameRoot)
        if ($null -ne $state) { $classifyArgs += @('--state', $statePath) }
        $classifyOutput = & $python @classifyArgs
        if ($LASTEXITCODE -ne 0) { throw 'Original archive classification failed.' }
        $classification = ($classifyOutput -join [Environment]::NewLine) | ConvertFrom-Json
        if (-not [bool]$classification.all_original) { throw 'One or more present archives are not original.' }
        Write-Host 'Original archives recorded by the active/saved manifest: OK'
    }

    Write-Host 'Verification completed successfully.'
    exit 0
} catch {
    Write-Error $_.Exception.Message
    exit 1
}
