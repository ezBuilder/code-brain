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
  "ai queue dead --json" \
  "ai obs health-summary --json" \
  "ai upgrade plan --target-version" \
  "exit code \`16\`" \
  "CI_READ_ONLY" \
  "release_ready" \
  "release-gate.yml" \
  "summary-observe" \
  "summary-parity.py" \
  "RELEASE_GATE_SUMMARY_SCHEMA_VERSION" \
  "dep-advisory.json" \
  "dep-advisory.sh" \
  "release-gate.summary.json" \
  "ai report release-gate-summary" \
  "queue status" \
  "oldest_pending_age_seconds" \
  "oldest_processing_age_seconds" \
  "worker stop --force" \
  "worker health" \
  "PRODUCTION_HARDENING_BACKLOG.md" \
  "./scripts/release-gate.sh" \
  "make env-check" \
  "make preflight" \
  "make lint" \
  "make release-gate" \
  "make clean-all" \
  "bootstrap.ps1" \
  "bootstrap-idempotency.sh" \
  "reproducibility-check.sh" \
  "release-notes.md"
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
uv run --project .ai/runtime ai obs health-summary --json >/dev/null
uv run --project .ai/runtime ai obs slo --json >/dev/null
uv run --project .ai/runtime ai queue status --json >/dev/null
uv run --project .ai/runtime ai queue dead --json --limit 1 >/dev/null
uv run --project .ai/runtime ai diagnostics bundle --dry-run --json >/dev/null
uv run --project .ai/runtime ai upgrade plan --target-version 0.1.1 --json >/dev/null
uv run --project .ai/runtime ai upgrade apply --target-version 0.1.1 --dry-run --json >/dev/null
uv run --project .ai/runtime ai report release-notes >/dev/null
uv run --project .ai/runtime ai report release-gate-summary --git-sha "$(git rev-parse HEAD)" --json >/dev/null
uv run --project .ai/runtime python -c 'from ai_core.report import RELEASE_GATE_SUMMARY_SCHEMA_VERSION; assert RELEASE_GATE_SUMMARY_SCHEMA_VERSION == 1'
CODE_BRAIN_DEP_ADVISORY_OFFLINE=1 ./scripts/dep-advisory.sh >/dev/null
./scripts/env-check.sh >/dev/null
./scripts/preflight.sh --check-only --json >/dev/null
make -n env-check >/dev/null
make -n preflight >/dev/null
make -n lint >/dev/null
make -n quick >/dev/null
make -n package >/dev/null
make -n verify-artifacts >/dev/null
make -n install-check >/dev/null
make -n reproducibility-check >/dev/null
make -n tamper-check >/dev/null
make -n rollback-drill >/dev/null
make -n bootstrap-idempotency >/dev/null
make -n release-gate >/dev/null
make -n clean-cache >/dev/null
make -n clean-artifacts >/dev/null
make -n clean-all >/dev/null

CI=true uv run --project .ai/runtime ai obs metrics --json >/dev/null
CI=true uv run --project .ai/runtime ai obs health-summary --json >/dev/null
CI=true uv run --project .ai/runtime ai diagnostics bundle --dry-run --json >/dev/null

set +e
CI=true uv run --project .ai/runtime ai worker stop --force --json >/tmp/code-brain-worker-stop-ci.out 2>/tmp/code-brain-worker-stop-ci.err
status=$?
set -e
if [[ "$status" -ne 16 ]]; then
  echo "expected CI worker stop rejection exit 16, got $status" >&2
  cat /tmp/code-brain-worker-stop-ci.err >&2
  exit 1
fi

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
  --exclude './.claude' \
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
./scripts/preflight.sh --check-only --json >/dev/null
uv run --project .ai/runtime ai queue recover-expired --json >/dev/null
uv run --project .ai/runtime ai queue archive-dead --older-than-days 30 --json >/dev/null
uv run --project .ai/runtime ai diagnostics bundle --json >/dev/null
uv run --project .ai/runtime ai diagnostics prune --keep-days 30 --json >/dev/null

echo "docs check ok"
