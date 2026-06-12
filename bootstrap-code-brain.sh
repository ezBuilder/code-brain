#!/usr/bin/env bash
set -euo pipefail
umask 077
cd "$(dirname "$0")"
./scripts/preflight.sh --check-only >/dev/null
./scripts/env-check.sh >/dev/null
uv sync --project .ai/runtime --extra dense
if git rev-parse --git-dir >/dev/null 2>&1; then
  git config core.hooksPath .githooks
fi
uv run --project .ai/runtime ai render --manifest-only --json >/dev/null
uv run --project .ai/runtime ai doctor --json >/dev/null
