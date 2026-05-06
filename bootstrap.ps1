$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
uv sync --project .ai/runtime
uv run --project .ai/runtime ai render --no-overwrite
uv run --project .ai/runtime ai doctor --strict

