$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
uv sync --project .ai/runtime
$IsCi = ($env:CI -match "^(1|true|yes)$") -or -not [string]::IsNullOrEmpty($env:GITHUB_ACTIONS)
if ($IsCi) {
    uv run --project .ai/runtime ai render --dry-run --json | Out-Null
} else {
    uv run --project .ai/runtime ai render
}
uv run --project .ai/runtime ai doctor --strict
uv run --project .ai/runtime python -m pytest .ai/runtime/tests
