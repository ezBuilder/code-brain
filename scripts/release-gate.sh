#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

./bootstrap.sh
./scripts/smoke.sh
./scripts/docs-check.sh
./scripts/package.sh >/tmp/code-brain-package.txt
./scripts/install-check.sh "$(head -n 1 /tmp/code-brain-package.txt)"
uv run --project .ai/runtime ai doctor --strict --json >/dev/null
uv run --project .ai/runtime ai report status --json >/dev/null

if [[ -n "$(git status --short)" ]]; then
  git status --short
  echo "release gate failed: tracked working tree is dirty" >&2
  exit 1
fi

echo "release gate ok"
