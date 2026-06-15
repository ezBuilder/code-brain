# Code Brain — one-command, zero-config installer (Windows).
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install.ps1 [TARGET_DIR]
#
# Ensures `uv` is present (auto-installs it), copies the repo-local runtime into
# <target>/.ai, wires the agent CLIs (Claude/Codex/Antigravity, all configured and inert
# if absent), and bootstraps the venv. The PowerShell installer writes no global
# config; the Unix peer can offer a managed global Claude/Codex merge.
$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"
$SourceRoot = (Resolve-Path "$PSScriptRoot/..").Path
$UvInstalledByInstaller = $false
if ($args.Count -ge 1) {
  $TargetArg = if ([System.IO.Path]::IsPathRooted($args[0])) { $args[0] } else { Join-Path (Get-Location).Path $args[0] }
  $TargetArg = [System.IO.Path]::GetFullPath($TargetArg)
  if (-not (Test-Path $TargetArg)) {
    New-Item -ItemType Directory -Force -Path $TargetArg | Out-Null
  }
  $Target = (Resolve-Path $TargetArg).Path
} else {
  $Target = (Get-Location).Path
}

function Invoke-Checked {
  param(
    [Parameter(Mandatory = $true)] [string] $FilePath,
    [string[]] $Arguments = @()
  )
  & $FilePath @Arguments
  if ($LASTEXITCODE -ne 0) {
    throw "[code-brain] command failed ($LASTEXITCODE): $FilePath $($Arguments -join ' ')"
  }
}

function Invoke-CheckedRetry {
  param(
    [Parameter(Mandatory = $true)] [string] $FilePath,
    [string[]] $Arguments = @(),
    [int] $Attempts = 2,
    [scriptblock] $BeforeRetry = $null
  )
  for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
    & $FilePath @Arguments
    if ($LASTEXITCODE -eq 0) { return }
    $code = $LASTEXITCODE
    if ($attempt -ge $Attempts) {
      throw "[code-brain] command failed ($code): $FilePath $($Arguments -join ' ')"
    }
    Write-Host "[code-brain] retrying after command failed ($code): $FilePath $($Arguments -join ' ')"
    if ($BeforeRetry) { & $BeforeRetry }
    Start-Sleep -Seconds 1
  }
}

# 1. uv is the only hard prerequisite (provisions Python itself). Auto-install if missing.
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Write-Host "[code-brain] 'uv' not found - installing from astral.sh ..."
  powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
  $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
  $UvInstalledByInstaller = $true
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  throw "[code-brain] uv install failed. Install it manually: https://docs.astral.sh/uv/ , then re-run."
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
  $GitCandidates = @(@(
    "$env:ProgramFiles\Git\cmd",
    "${env:ProgramFiles(x86)}\Git\cmd",
    "$env:LOCALAPPDATA\Programs\Git\cmd"
  ) | Where-Object { $_ -and (Test-Path (Join-Path $_ "git.exe")) })
  if ($GitCandidates.Count -gt 0) {
    $env:Path = "$($GitCandidates[0]);$env:Path"
  }
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
  throw "[code-brain] git is required. Install Git for Windows, then re-run."
}

Push-Location $Target
try {
  if (-not (Test-Path (Join-Path $Target ".git"))) {
    Invoke-Checked "git" @("init")
  }
} finally {
  Pop-Location
}

# 2. Copy + wire repo-local config (repo-local only; never global).
Write-Host "[code-brain] installing into: $Target"
Invoke-Checked "powershell" @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "$SourceRoot/scripts/install-into.ps1", "install", "$Target")

# 3. Bootstrap the runtime, verify strict health, and create the first session snapshot.
Push-Location $Target
try {
  Invoke-Checked "uv" @("sync", "--project", ".ai/runtime", "--extra", "dense")
  Invoke-Checked "uv" @("run", "--project", ".ai/runtime", "ai", "render", "--manifest-only", "--json") | Out-Null
  Invoke-Checked "uv" @("run", "--project", ".ai/runtime", "ai", "index", "rebuild", "--json") | Out-Null
  Invoke-CheckedRetry "uv" @("run", "--project", ".ai/runtime", "ai", "doctor", "--strict", "--json") -BeforeRetry {
    Invoke-Checked "uv" @("run", "--project", ".ai/runtime", "ai", "index", "rebuild", "--json") | Out-Null
  } | Out-Null
  Invoke-Checked "uv" @("run", "--project", ".ai/runtime", "ai", "session", "start", "--agent", "installer", "--query", "initial Code Brain setup", "--json") | Out-Null
} finally {
  Pop-Location
}

Write-Host "[code-brain] installed. New AI sessions in $Target now load Code Brain memory, search, hooks, and MCP automatically."
if ($UvInstalledByInstaller) {
  Write-Host "[code-brain] note: uv was installed at $env:USERPROFILE\.local\bin. Open a new PowerShell session if the 'uv' command is not visible in the current shell."
}
