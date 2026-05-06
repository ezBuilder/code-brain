#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
uv sync --project .ai/runtime
uv run --project .ai/runtime ai render
uv run --project .ai/runtime ai doctor --strict
uv run --project .ai/runtime python -m pytest .ai/runtime/tests
