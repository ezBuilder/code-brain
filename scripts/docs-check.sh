#!/usr/bin/env bash
set -euo pipefail
export COPYFILE_DISABLE=1

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

cd "$ROOT"
unset CI GITHUB_ACTIONS

if [[ ! -f OPERATIONS.md ]]; then
  echo "OPERATIONS.md is missing" >&2
  exit 1
fi

for needle in \
  "ai doctor --strict --json" \
  "ai report status --json" \
  "ai diagnostics bundle --dry-run --json" \
  "ai queue recover-expired --json" \
  "ai upgrade plan --target-version" \
  "exit code \`16\`" \
  "./scripts/release-gate.sh" \
  "make env-check" \
  "make lint" \
  "make release-gate"
do
  if ! grep -Fq "$needle" OPERATIONS.md README.md RELEASE.md; then
    echo "documented operation missing: $needle" >&2
    exit 1
  fi
done

uv run --project .ai/runtime ai version >/dev/null
uv run --project .ai/runtime ai doctor --strict --json >/dev/null
uv run --project .ai/runtime ai report status --json >/dev/null
uv run --project .ai/runtime ai obs metrics --json >/dev/null
uv run --project .ai/runtime ai obs slo --json >/dev/null
uv run --project .ai/runtime ai queue status --json >/dev/null
uv run --project .ai/runtime ai diagnostics bundle --dry-run --json >/dev/null
uv run --project .ai/runtime ai upgrade plan --target-version 0.1.1 --json >/dev/null
uv run --project .ai/runtime ai upgrade apply --target-version 0.1.1 --dry-run --json >/dev/null
uv run --project .ai/runtime ai report release-notes >/dev/null
./scripts/env-check.sh >/dev/null
make -n env-check >/dev/null
make -n lint >/dev/null
make -n quick >/dev/null
make -n package >/dev/null
make -n verify-artifacts >/dev/null
make -n install-check >/dev/null
make -n tamper-check >/dev/null
make -n release-gate >/dev/null

CI=true uv run --project .ai/runtime ai obs metrics --json >/dev/null
CI=true uv run --project .ai/runtime ai diagnostics bundle --dry-run --json >/dev/null

set +e
CI=true uv run --project .ai/runtime ai render >/tmp/code-brain-docs-ci-write.out 2>/tmp/code-brain-docs-ci-write.err
status=$?
set -e
if [[ "$status" -ne 16 ]]; then
  echo "expected CI write rejection exit 16, got $status" >&2
  cat /tmp/code-brain-docs-ci-write.err >&2
  exit 1
fi

COPY="$TMP/code-brain"
mkdir -p "$COPY"
tar \
  --exclude './.git' \
  --exclude './.ai/cache' \
  --exclude './.ai/runtime/.venv' \
  --exclude './.ai/runtime/.pytest_cache' \
  --exclude './.ai/runtime/src/ai_core/__pycache__' \
  --exclude './.ai/runtime/src/ai_core/worker/__pycache__' \
  --exclude './.ai/runtime/tests/__pycache__' \
  --exclude './dist' \
  -C "$ROOT" -cf - . | tar -C "$COPY" -xf -

cd "$COPY"
unset CI GITHUB_ACTIONS
uv run --project .ai/runtime ai render --json >/dev/null
uv run --project .ai/runtime ai queue recover-expired --json >/dev/null
uv run --project .ai/runtime ai queue archive-dead --older-than-days 30 --json >/dev/null
uv run --project .ai/runtime ai diagnostics bundle --json >/dev/null
uv run --project .ai/runtime ai diagnostics prune --keep-days 30 --json >/dev/null

echo "docs check ok"
