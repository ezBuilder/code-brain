#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PACKAGE_OUTPUT="$(mktemp)"
trap 'rm -f "$PACKAGE_OUTPUT"' EXIT

./scripts/env-check.sh >/dev/null
./scripts/lint.sh
./bootstrap.sh
./scripts/smoke.sh
./scripts/docs-check.sh
./scripts/package.sh >"$PACKAGE_OUTPUT"
ARCHIVE="$(head -n 1 "$PACKAGE_OUTPUT")"
if [[ -z "$ARCHIVE" || ! -f "$ARCHIVE" ]]; then
  cat "$PACKAGE_OUTPUT" >&2
  echo "release gate failed: package script did not emit an archive path" >&2
  exit 1
fi
./scripts/verify-artifacts.sh "$ARCHIVE" >/dev/null
./scripts/install-check.sh "$ARCHIVE"
./scripts/artifact-tamper-check.sh "$ARCHIVE"
uv run --project .ai/runtime ai doctor --strict --json >/dev/null
uv run --project .ai/runtime ai report status --json >/dev/null

if [[ -n "$(git status --short)" ]]; then
  git status --short
  echo "release gate failed: tracked working tree is dirty" >&2
  exit 1
fi

echo "release gate ok"
