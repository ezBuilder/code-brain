#!/usr/bin/env pwsh
# Runtime activation used by install-into.sh while its rollback transaction is open.

param(
    [Parameter(Mandatory = $true)] [string] $TargetRoot
)

$ErrorActionPreference = "Stop"
$TargetRoot = (Resolve-Path $TargetRoot).Path

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)] [string] $FilePath,
        [string[]] $Arguments = @()
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "command failed ($LASTEXITCODE): $FilePath $($Arguments -join ' ')"
    }
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "install-into failed: uv is required for Windows runtime activation"
}

$Venv = Join-Path $TargetRoot ".ai/runtime/.venv"
$VenvExisted = Test-Path -LiteralPath $Venv
$PreviousHooksPath = (& git -C $TargetRoot config --get core.hooksPath 2>$null)
$HadHooksPath = $LASTEXITCODE -eq 0

Push-Location $TargetRoot
try {
    if (-not $env:UV_CONCURRENT_DOWNLOADS) { $env:UV_CONCURRENT_DOWNLOADS = "1" }
    if (-not $env:UV_CONCURRENT_BUILDS) { $env:UV_CONCURRENT_BUILDS = "1" }
    if (-not $env:UV_CONCURRENT_INSTALLS) { $env:UV_CONCURRENT_INSTALLS = "1" }

    New-Item -ItemType Directory -Force -Path ".ai/cache" | Out-Null
    Invoke-Checked "uv" @("sync", "--no-progress", "--project", ".ai/runtime")
    Invoke-Checked "git" @("config", "core.hooksPath", ".githooks")
    Invoke-Checked "uv" @("run", "--project", ".ai/runtime", "ai", "audit", "repair-chain", "--json")
    Invoke-Checked "uv" @("run", "--project", ".ai/runtime", "ai", "render", "--manifest-only", "--json")
    Invoke-Checked "uv" @("run", "--project", ".ai/runtime", "ai", "index", "rebuild", "--json")

    $DoctorArgs = @("run", "--project", ".ai/runtime", "ai", "doctor")
    if ($env:AI_INSTALL_STRICT -match '^(1|true|yes|on)$') { $DoctorArgs += "--strict" }
    $DoctorArgs += "--json"
    Invoke-Checked "uv" $DoctorArgs

    Invoke-Checked "uv" @(
        "run", "--project", ".ai/runtime", "ai", "session", "start",
        "--agent", "installer", "--rebuild", "auto", "--repair-audit-index",
        "--render-manifest", "--json"
    )
}
catch {
    if ($HadHooksPath) {
        & git config core.hooksPath $PreviousHooksPath 2>$null
    }
    else {
        & git config --unset-all core.hooksPath 2>$null
    }
    if (-not $VenvExisted -and (Test-Path -LiteralPath $Venv)) {
        Remove-Item -LiteralPath $Venv -Recurse -Force -ErrorAction SilentlyContinue
    }
    throw
}
finally {
    Pop-Location
}
