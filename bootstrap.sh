#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
./scripts/env-check.sh >/dev/null
uv sync --project .ai/runtime
if [[ "${CI:-}" =~ ^(1|true|yes)$ || -n "${GITHUB_ACTIONS:-}" ]]; then
  uv run --project .ai/runtime ai render --dry-run --json >/dev/null
else
  uv run --project .ai/runtime ai render
fi
uv run --project .ai/runtime ai doctor --strict
uv run --project .ai/runtime python -m pytest .ai/runtime/tests
