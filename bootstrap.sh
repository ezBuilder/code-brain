#!/usr/bin/env bash
set -euo pipefail
umask 077
cd "$(dirname "$0")"
./scripts/preflight.sh --check-only >/dev/null
./scripts/env-check.sh >/dev/null
uv sync --project .ai/runtime
if [[ "${CI:-}" =~ ^(1|true|yes)$ || -n "${GITHUB_ACTIONS:-}" ]]; then
  uv run --project .ai/runtime ai render --dry-run --json >/dev/null
else
  uv run --project .ai/runtime ai render
fi
uv run --project .ai/runtime ai doctor --strict
if [[ ! "${AI_BOOTSTRAP_SKIP_TESTS:-}" =~ ^(1|true|yes)$ ]]; then
  env -u CI -u GITHUB_ACTIONS -u GITLAB_CI -u AI_CI uv run --project .ai/runtime python -m pytest .ai/runtime/tests
fi
