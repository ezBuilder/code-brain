#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .ai/runtime/uv.lock ]]; then
  echo "lockfile-check failed: missing .ai/runtime/uv.lock" >&2
  echo "remediation: run uv lock --project .ai/runtime and commit .ai/runtime/uv.lock" >&2
  exit 1
fi

set +e
output="$(uv lock --check --project .ai/runtime 2>&1)"
status=$?
set -e

if [[ "$status" -ne 0 ]]; then
  echo "lockfile-check failed: uv.lock is out of sync with .ai/runtime/pyproject.toml" >&2
  if [[ -n "$output" ]]; then
    printf '%s\n' "$output" >&2
  fi
  echo "remediation: run uv lock --project .ai/runtime and commit .ai/runtime/uv.lock" >&2
  exit "$status"
fi

echo "lockfile-check ok"
