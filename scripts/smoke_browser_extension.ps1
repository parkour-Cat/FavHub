<#
.SYNOPSIS
Launch Chrome with the FavHub extension in a throwaway profile.

.DESCRIPTION
A smoke run that touches nothing the user owns. The extension is loaded into a
fresh profile under an explicit temporary root, so the user's real profile,
sessions, and cookies are never involved — and neither is anything this script
could accidentally delete.

The script supplies no credentials and bypasses no login. Whoever runs it signs
in by hand, in the throwaway profile, exactly as they would in any browser. A
smoke run that logged itself in would prove nothing about the path real users
take, and would mean handling a password that this script has no business
seeing.

.PARAMETER ChromePath
Chrome executable. Defaults to the usual install locations.

.PARAMETER ExtensionDir
Unpacked extension directory. Defaults to the one `favhub setup` installs.

.PARAMETER ProfileRoot
Temporary root for the throwaway profile. Everything created lives under here,
and only paths under here are ever removed.

.PARAMETER KeepProfile
Leave the profile behind for inspection instead of deleting it.

.EXAMPLE
pwsh -File scripts/smoke_browser_extension.ps1
#>

[CmdletBinding()]
param(
    [string]$ChromePath,
    [string]$ExtensionDir = (Join-Path $env:LOCALAPPDATA 'FavHub\extension'),
    [string]$ProfileRoot = (Join-Path $env:TEMP 'favhub-extension-smoke'),
    [switch]$KeepProfile
)

$ErrorActionPreference = 'Stop'

function Resolve-Chrome {
    param([string]$Explicit)
    if ($Explicit) {
        if (-not (Test-Path -LiteralPath $Explicit -PathType Leaf)) {
            throw "Chrome not found at: $Explicit"
        }
        return (Resolve-Path -LiteralPath $Explicit).Path
    }
    $candidates = @(
        (Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe'),
        (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe')
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    throw 'Chrome not found. Pass -ChromePath explicitly.'
}

# Deleting a directory tree is the one irreversible thing here, so the target is
# proved to live under the root this script created rather than merely looking
# like it. A relative path, a symlink, or a "..\" in a parameter would otherwise
# be enough to aim the cleanup at something the user cares about.
function Assert-Contained {
    param([string]$Path, [string]$Root)
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $fullRoot = [System.IO.Path]::GetFullPath($Root)
    if (-not $fullRoot.EndsWith([System.IO.Path]::DirectorySeparatorChar)) {
        $fullRoot += [System.IO.Path]::DirectorySeparatorChar
    }
    if (-not $fullPath.StartsWith($fullRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to touch a path outside the temporary root: $fullPath"
    }
    return $fullPath
}

$chrome = Resolve-Chrome -Explicit $ChromePath

if (-not (Test-Path -LiteralPath $ExtensionDir -PathType Container)) {
    throw "Extension directory not found: $ExtensionDir`nRun 'favhub setup' first."
}
$extension = (Resolve-Path -LiteralPath $ExtensionDir).Path
foreach ($required in @('manifest.json', 'background.js', 'EXTENSION_ID')) {
    if (-not (Test-Path -LiteralPath (Join-Path $extension $required) -PathType Leaf)) {
        throw "Extension directory is incomplete, missing: $required"
    }
}

$null = New-Item -ItemType Directory -Force -Path $ProfileRoot
$root = (Resolve-Path -LiteralPath $ProfileRoot).Path
$profileDir = Assert-Contained -Path (Join-Path $root ("profile-" + [Guid]::NewGuid().ToString('N'))) -Root $root
$null = New-Item -ItemType Directory -Force -Path $profileDir

$expectedId = (Get-Content -LiteralPath (Join-Path $extension 'EXTENSION_ID') -Raw).Trim()

Write-Host "Chrome:     $chrome"
Write-Host "Extension:  $extension"
Write-Host "Profile:    $profileDir"
Write-Host "Pinned id:  $expectedId"
Write-Host ''
Write-Host 'A throwaway profile is starting. Sign in by hand to smoke a real run;'
Write-Host 'this script supplies no credentials and bypasses no login.'
Write-Host 'Close Chrome when finished.'
Write-Host ''

$arguments = @(
    "--user-data-dir=$profileDir"
    "--load-extension=$extension"
    '--no-first-run'
    '--no-default-browser-check'
    'chrome://extensions'
)

try {
    $process = Start-Process -FilePath $chrome -ArgumentList $arguments -PassThru
    $process.WaitForExit()
    Write-Host "Chrome exited with code $($process.ExitCode)."
}
finally {
    if ($KeepProfile) {
        Write-Host "Profile kept at: $profileDir"
    }
    else {
        # Re-checked rather than trusted: the guard above ran before Chrome did.
        $target = Assert-Contained -Path $profileDir -Root $root
        Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host 'Throwaway profile removed.'
    }
}
