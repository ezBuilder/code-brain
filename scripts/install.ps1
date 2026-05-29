# Code Brain — one-command, zero-config installer (Windows).
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install.ps1 [TARGET_DIR]
#
# Ensures `uv` is present (auto-installs it), copies the repo-local runtime into
# <target>/.ai, wires the agent CLIs (Claude/Codex/Antigravity, all configured and inert
# if absent), and bootstraps the venv. Writes NOTHING to global config. Unix peer:
# scripts/install.sh.
$ErrorActionPreference = "Stop"
$SourceRoot = (Resolve-Path "$PSScriptRoot/..").Path
$Target = if ($args.Count -ge 1) { (Resolve-Path $args[0]).Path } else { (Get-Location).Path }

# 1. uv is the only hard prerequisite (provisions Python itself). Auto-install if missing.
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Write-Host "[code-brain] 'uv' not found - installing from astral.sh ..."
  powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
  $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  throw "[code-brain] uv install failed. Install it manually: https://docs.astral.sh/uv/ , then re-run."
}

# 2. Copy + wire repo-local config (repo-local only; never global).
Write-Host "[code-brain] installing into: $Target"
& powershell -NoProfile -ExecutionPolicy Bypass -File "$SourceRoot/scripts/install-into.ps1" install "$Target"

# 3. Bootstrap the runtime (uv sync -> venv, manifest, doctor).
Push-Location $Target
try {
  uv sync --project .ai/runtime --extra dense
  uv run --project .ai/runtime ai render --manifest-only --json | Out-Null
  uv run --project .ai/runtime ai doctor --json | Out-Null
} finally {
  Pop-Location
}

Write-Host "[code-brain] done. claude / codex / agy in $Target now share Code Brain memory."
