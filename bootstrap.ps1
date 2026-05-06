$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
uv sync --project .ai/runtime
uv run --project .ai/runtime ai render
uv run --project .ai/runtime ai doctor --strict
uv run --project .ai/runtime python -m pytest .ai/runtime/tests
