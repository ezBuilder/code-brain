#!/usr/bin/env pwsh
# Code Brain Windows installer.
#
# Git for Windows ships the POSIX tools required by install-into.sh. Delegate
# mutation to that single transactional implementation so Windows cannot drift
# into a second, weaker ownership/config/uninstall contract.

param(
    [Parameter(Position = 0)] [string] $Action = "",
    [Parameter(Position = 1)] [string] $TargetArg = ""
)

$ErrorActionPreference = "Stop"
$SourceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

function Fail-Install {
    param([string] $Message, [int] $Code = 2)
    [Console]::Error.WriteLine("install-into failed: $Message")
    exit $Code
}

if ($Action -eq "-h" -or $Action -eq "--help") {
    Write-Host "usage:"
    Write-Host "  scripts/install-into.ps1 <target-git-repo>"
    Write-Host "  scripts/install-into.ps1 install <target>"
    Write-Host "  scripts/install-into.ps1 upgrade <target>"
    Write-Host "  scripts/install-into.ps1 uninstall <target>"
    exit 2
}

if ($Action -in @("install", "upgrade", "uninstall")) {
    if (-not $TargetArg) { Fail-Install "target argument required" }
}
elseif ($Action) {
    $TargetArg = $Action
    $Action = "install"
}
else {
    Fail-Install "target argument required"
}

if (-not (Test-Path -LiteralPath $TargetArg -PathType Container)) {
    Fail-Install "target does not exist: $TargetArg"
}
$TargetRoot = (Resolve-Path -LiteralPath $TargetArg).Path

$Git = Get-Command git.exe -ErrorAction SilentlyContinue
if (-not $Git) { $Git = Get-Command git -ErrorAction SilentlyContinue }
if (-not $Git) { Fail-Install "git is required" }

$GitTop = (& $Git.Source -C $TargetRoot rev-parse --show-toplevel 2>$null)
if ($LASTEXITCODE -ne 0 -or -not $GitTop) {
    Fail-Install "target is not inside a git repository: $TargetRoot"
}
$GitTopPath = [System.IO.Path]::GetFullPath(($GitTop | Select-Object -First 1).Trim())
$TargetPath = [System.IO.Path]::GetFullPath($TargetRoot)
if (-not [System.StringComparer]::OrdinalIgnoreCase.Equals(
    $GitTopPath.TrimEnd([char[]]"\/"),
    $TargetPath.TrimEnd([char[]]"\/")
)) {
    Fail-Install "pass the git repository root: $GitTopPath"
}

$GitDir = Split-Path $Git.Source -Parent
$BashCandidates = @(
    (Join-Path $GitDir "bash.exe"),
    (Join-Path $GitDir "..\bin\bash.exe"),
    (Join-Path $GitDir "..\usr\bin\bash.exe"),
    (Join-Path $env:ProgramFiles "Git\bin\bash.exe"),
    (Join-Path $env:ProgramFiles "Git\usr\bin\bash.exe")
)
if (${env:ProgramFiles(x86)}) {
    $BashCandidates += Join-Path ${env:ProgramFiles(x86)} "Git\bin\bash.exe"
}
$Bash = $BashCandidates |
    Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } |
    Select-Object -First 1
if (-not $Bash) {
    Fail-Install "Git for Windows bash.exe was not found; repair the Git for Windows installation"
}
$Bash = (Resolve-Path -LiteralPath $Bash).Path

$BashDir = Split-Path $Bash -Parent
$CygpathCandidates = @(
    (Join-Path $BashDir "cygpath.exe"),
    (Join-Path $BashDir "..\usr\bin\cygpath.exe")
)
$Cygpath = $CygpathCandidates |
    Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
    Select-Object -First 1
if (-not $Cygpath) {
    Fail-Install "Git for Windows cygpath.exe was not found"
}
$Cygpath = (Resolve-Path -LiteralPath $Cygpath).Path

$SourceUnix = (& $Cygpath -u $SourceRoot)
if ($LASTEXITCODE -ne 0 -or -not $SourceUnix) { Fail-Install "could not convert source path for Git Bash" }
$TargetUnix = (& $Cygpath -u $TargetRoot)
if ($LASTEXITCODE -ne 0 -or -not $TargetUnix) { Fail-Install "could not convert target path for Git Bash" }
$SourceUnix = ($SourceUnix | Select-Object -First 1).Trim()
$TargetUnix = ($TargetUnix | Select-Object -First 1).Trim()
$InstallScript = "$SourceUnix/scripts/install-into.sh"

$PreviousTargetWindows = $env:AI_INSTALL_TARGET_WINDOWS
try {
    $env:AI_INSTALL_TARGET_WINDOWS = "1"
    & $Bash "--noprofile" "--norc" $InstallScript $Action $TargetUnix
    $Code = $LASTEXITCODE
}
finally {
    if ($null -eq $PreviousTargetWindows) {
        Remove-Item Env:AI_INSTALL_TARGET_WINDOWS -ErrorAction SilentlyContinue
    }
    else {
        $env:AI_INSTALL_TARGET_WINDOWS = $PreviousTargetWindows
    }
}

exit $Code
