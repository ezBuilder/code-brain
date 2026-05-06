#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
uv sync --project .ai/runtime
uv run --project .ai/runtime ai render --no-overwrite
uv run --project .ai/runtime ai doctor --strict

