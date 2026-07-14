[CmdletBinding()]
param(
    [ValidateSet('DualSense', 'DualShock4')]
    [string]$Variant = 'DualSense'
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
    if (-not (Test-Path -LiteralPath $gameExe -PathType Leaf)) {
        throw 'Place this project directly inside the supported Danganronpa V3 game directory.'
    }
    if (Get-Process -Name 'Dangan3Win' -ErrorAction SilentlyContinue) {
        throw 'Danganronpa V3 is running. Close the game before installing or switching variants.'
    }
    if ((Get-Item -LiteralPath $gameExe).Length -ne [int64]$baseManifest.supported_game.executable.size -or
        (Get-Sha256 $gameExe) -ne $baseManifest.supported_game.executable.sha256.ToUpperInvariant()) {
        throw 'Unsupported or changed Dangan3Win.exe. No archive was modified.'
    }

    $languagePath = Join-Path $gameRoot 'language.txt'
    $language = if (Test-Path -LiteralPath $languagePath) {
        (Get-Content -Raw -LiteralPath $languagePath).Trim().ToUpperInvariant()
    } else { '' }
    if ([string]::IsNullOrWhiteSpace($language) -or $language -notmatch '^[A-Z0-9_]+$') {
        throw "Could not determine a valid active language code from language.txt: '$language'."
    }

    $python = (Get-Command python -ErrorAction Stop).Source
    $modTool = Join-Path $projectRoot 'tools\modtool.py'
    $assetRoot = Join-Path $projectRoot 'assets'
    if (-not (Test-Path -LiteralPath $modTool -PathType Leaf)) { throw 'Missing tools\modtool.py.' }
    foreach ($asset in $baseManifest.redistributable_assets) {
        $assetPath = Resolve-RelativePath $projectRoot $asset.path
        if (-not (Test-Path -LiteralPath $assetPath -PathType Leaf) -or
            ($asset.size -and (Get-Item -LiteralPath $assetPath).Length -ne [int64]$asset.size) -or
            (Get-Sha256 $assetPath) -ne $asset.sha256.ToUpperInvariant()) {
            throw "Project asset verification failed: $($asset.path)"
        }
    }

    $localRoot = Join-Path $gameRoot 'DualSense_UI_Mod_Data'
    $originalEntryRoot = Join-Path $localRoot 'original_entries'
    $runtimeManifestRoot = Join-Path $localRoot 'manifests'
    $statePath = Join-Path $localRoot 'installed-state.json'
    New-Item -ItemType Directory -Force -Path $originalEntryRoot,$runtimeManifestRoot | Out-Null
    $state = if (Test-Path -LiteralPath $statePath -PathType Leaf) {
        Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json
    } else { $null }

    $activeGamePath = $baseManifest.language_archive.game_path_pattern.Replace('{language_lower}', $language.ToLowerInvariant()).Replace('{language}', $language)
    $activeArchivePath = Resolve-RelativePath $gameRoot $activeGamePath
    if (-not (Test-Path -LiteralPath $activeArchivePath -PathType Leaf)) {
        throw "The active $language language archive is missing: $activeGamePath"
    }
    $runtimeManifestPath = Join-Path $runtimeManifestRoot ("runtime-manifest-$language.json")
    $reuseRuntimeManifest = $false
    if (Test-Path -LiteralPath $runtimeManifestPath -PathType Leaf) {
        $candidate = Get-Content -Raw -LiteralPath $runtimeManifestPath | ConvertFrom-Json
        $candidatePatch = $candidate.archive_patches | Where-Object id -eq 'language_controller_help' | Select-Object -First 1
        $candidateScrumPatch = $candidate.archive_patches | Where-Object id -eq 'language_scrum_prompts' | Select-Object -First 1
        if ($null -ne $candidatePatch -and
            $null -ne $candidateScrumPatch -and
            $candidatePatch.game_path -eq $activeGamePath -and
            $candidateScrumPatch.game_path -eq $activeGamePath) {
            $classifyArgs = @($modTool, 'classify', '--manifest', $runtimeManifestPath, '--game-root', $gameRoot, '--entries-only')
            if ($null -ne $state) { $classifyArgs += @('--state', $statePath) }
            $candidateOutput = & $python @classifyArgs 2>$null
            if ($LASTEXITCODE -eq 0) {
                $reuseRuntimeManifest = $true
            }
        }
    }
    if (-not $reuseRuntimeManifest) {
        Write-Host "Discovering the active $language controller-help and Scrum Debate resources..."
        $prepareArgs = @(
            $modTool, 'prepare-language',
            '--manifest', $baseManifestPath,
            '--game-root', $gameRoot,
            '--language', $language,
            '--output', $runtimeManifestPath
        )
        if (Test-Path -LiteralPath $runtimeManifestPath -PathType Leaf) {
            $prepareArgs += @('--existing-manifest', $runtimeManifestPath)
        }
        if ($null -ne $state) { $prepareArgs += @('--state', $statePath) }
        & $python @prepareArgs
        if ($LASTEXITCODE -ne 0) {
            throw "The $language language pack is unsupported or already modified. Use Steam Verify, then retry."
        }
    }

    $manifestPath = $runtimeManifestPath
    $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
    if ($manifest.supported_game.language_code -ne $language) { throw 'Runtime language manifest mismatch.' }

    Write-Host "Classifying supported resident and $language language archives..."
    $classifyArgs = @($modTool, 'classify', '--manifest', $manifestPath, '--game-root', $gameRoot, '--entries-only')
    if ($null -ne $state) { $classifyArgs += @('--state', $statePath) }
    $classifyOutput = & $python @classifyArgs
    if ($LASTEXITCODE -ne 0) { throw 'Unsupported archive state. Use Steam Verify before installing.' }
    $classification = ($classifyOutput -join [Environment]::NewLine) | ConvertFrom-Json
    $anyPatched = [bool]$classification.any_patched
    $allPatched = [bool]$classification.all_patched
    $plans = foreach ($patch in $manifest.archive_patches) {
        [PSCustomObject]@{
            Patch = $patch
            ArchivePath = Resolve-RelativePath $gameRoot $patch.game_path
        }
    }

    Write-Host 'Saving or verifying only the required compact original entries...'
    & $python $modTool capture-originals --manifest $manifestPath --game-root $gameRoot --output-dir $originalEntryRoot
    if ($LASTEXITCODE -ne 0) { throw 'Compact original-entry capture or verification failed.' }

    function Invoke-Build([string]$VariantKey, [string]$OutputDir) {
        & $python $modTool build `
            --manifest $manifestPath `
            --original-entry-dir $originalEntryRoot `
            --asset-dir $assetRoot `
            --variant $VariantKey `
            --output-dir $OutputDir
        if ($LASTEXITCODE -ne 0) { throw "Source payload build failed with exit code $LASTEXITCODE." }
    }

    $variantKey = if ($Variant -eq 'DualSense') { 'dualsense' } else { 'dualshock4' }
    $buildRoot = Join-Path $localRoot ("build\$language\" + $variantKey)
    Write-Host "Building the $Variant payload for language $language..."
    Invoke-Build $variantKey $buildRoot

    if ($allPatched -and $null -ne $state -and $state.variant -eq $Variant -and $state.language_code -eq $language) {
        $fullClassifyOutput = & $python $modTool classify --manifest $manifestPath --game-root $gameRoot --state $statePath
        if ($LASTEXITCODE -ne 0) { throw 'Installed archive checksum verification failed.' }
        $fullClassification = ($fullClassifyOutput -join [Environment]::NewLine) | ConvertFrom-Json
        if (-not [bool]$fullClassification.all_patched) { throw 'Installed archive state is incomplete.' }
        & $python $modTool verify --manifest $manifestPath --game-root $gameRoot --payload-dir $buildRoot --expect patched
        if ($LASTEXITCODE -ne 0) { throw 'Installed-state structural verification failed.' }
        Write-Host "$Variant is already installed and verified for $language."
        exit 0
    }

    $switching = $anyPatched -and $null -ne $state -and $state.variant -ne $Variant
    $currentBuildRoot = $null
    if ($switching) {
        $currentVariantKey = if ($state.variant -eq 'DualSense') { 'dualsense' } else { 'dualshock4' }
        $currentBuildRoot = Join-Path $localRoot ("build\$language\" + $currentVariantKey)
        Invoke-Build $currentVariantKey $currentBuildRoot
    }

    $patchAttempted = $false
    try {
        if ($switching) {
            Write-Host "Restoring compact originals before switching from $($state.variant) to $Variant..."
            & $python $modTool restore --manifest $manifestPath --game-root $gameRoot --payload-dir $currentBuildRoot --original-entry-dir $originalEntryRoot
            if ($LASTEXITCODE -ne 0) { throw 'Compact entry restoration failed while switching variants.' }
        }

        $patchAttempted = $true
        Write-Host "Applying the resident, $language controller-help, and Scrum Debate replacements..."
        $patchOutput = & $python $modTool patch --manifest $manifestPath --game-root $gameRoot --payload-dir $buildRoot
        if ($LASTEXITCODE -ne 0) { throw "Archive patching failed with exit code $LASTEXITCODE." }
        $patchResult = ($patchOutput -join [Environment]::NewLine) | ConvertFrom-Json
        Write-Host ($patchOutput -join [Environment]::NewLine)
        & $python $modTool verify --manifest $manifestPath --game-root $gameRoot --payload-dir $buildRoot --expect patched
        if ($LASTEXITCODE -ne 0) { throw 'Post-install structural verification failed.' }

        $records = @()
        foreach ($plan in $plans) {
            $payloadPath = Join-Path $buildRoot $plan.Patch.payload_name
            $patchRecord = $patchResult.archives | Where-Object {
                $_.game_path -eq $plan.Patch.game_path -and
                $_.target_entry -eq $plan.Patch.target_entry.path
            } | Select-Object -First 1
            if ($null -eq $patchRecord) { throw "Patcher did not report $($plan.Patch.game_path)." }
            $records += [ordered]@{
                game_path = $plan.Patch.game_path
                original_sha256 = $plan.Patch.original_archive_sha256
                patched_sha256 = $patchRecord.archive_sha256
                target_entry = $plan.Patch.target_entry.path
                original_entry_backup = $plan.Patch.target_entry.backup_name
                patched_entry_size = (Get-Item -LiteralPath $payloadPath).Length
                patched_entry_sha256 = Get-Sha256 $payloadPath
            }
        }
        [ordered]@{
            project = $manifest.project
            version = $manifest.version
            variant = $Variant
            language_code = $language
            runtime_manifest = "manifests/runtime-manifest-$language.json"
            installed = $true
            installed_utc = (Get-Date).ToUniversalTime().ToString('o')
            archives = $records
        } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $statePath -Encoding UTF8
    } catch {
        $failure = $_
        $recoveryRoot = if ($patchAttempted) { $buildRoot } elseif ($switching) { $currentBuildRoot } else { $null }
        if ($null -ne $recoveryRoot) {
            Write-Warning 'Installation failed; restoring the compact original entries.'
            & $python $modTool restore --manifest $manifestPath --game-root $gameRoot --payload-dir $recoveryRoot --original-entry-dir $originalEntryRoot
            if ($LASTEXITCODE -eq 0 -and $null -ne $state) {
                $state.installed = $false
                $state | Add-Member -NotePropertyName recovery_utc -NotePropertyValue ((Get-Date).ToUniversalTime().ToString('o')) -Force
                $state | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $statePath -Encoding UTF8
            }
        }
        throw $failure
    }

    Write-Host "$Variant UI variant installed successfully for language $language. Controller input was not changed."
    exit 0
} catch {
    Write-Error $_.Exception.Message
    exit 1
}
