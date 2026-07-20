#!/usr/bin/env bash
set -euo pipefail
export COPYFILE_DISABLE=1

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
QUIET_LOG="$TMP/quiet.log"
trap 'rm -rf "$TMP"' EXIT
JOB_OUTPUT="$TMP/job.json"
LEASE_OUTPUT="$TMP/lease.json"
APPROVAL_OUTPUT="$TMP/approval.json"
PACKAGE_OUTPUT="$TMP/package.txt"
PACKAGE_ERROR="$TMP/package.err"

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
  -C "$ROOT" -cf - . | tar -C "$COPY" -xf -

cd "$COPY"
unset CI GITHUB_ACTIONS GITLAB_CI AI_CI

# Release gate already runs the full suite in a git checkout. This detached copy
# smoke intentionally exercises install/runtime commands without git metadata.
uv run --project .ai/runtime ai doctor --strict --json >"$QUIET_LOG"
uv run --project .ai/runtime ai render --dry-run --json >"$QUIET_LOG"
uv run --project .ai/runtime ai index rebuild --json >"$QUIET_LOG"
uv run --project .ai/runtime ai code query worker --json >"$QUIET_LOG"
printf '{"agent":"codex"}' | uv run --project .ai/runtime ai hook SessionStart --json >"$QUIET_LOG"
printf '{"task":"smoke"}' | uv run --project .ai/runtime ai queue enqueue --priority P2 --kind smoke --json >"$JOB_OUTPUT"
JOB_ID="$(uv run --project .ai/runtime python -c 'import json,sys; print(json.load(open(sys.argv[1]))["job"]["id"])' "$JOB_OUTPUT")"
uv run --project .ai/runtime ai queue lease --worker-id smoke --json >"$LEASE_OUTPUT"
LEASE_ID="$(uv run --project .ai/runtime python -c 'import json,sys; print(json.load(open(sys.argv[1]))["job"]["lease_id"])' "$LEASE_OUTPUT")"
uv run --project .ai/runtime ai queue complete --job-id "$JOB_ID" --lease-id "$LEASE_ID" --json >"$QUIET_LOG"
uv run --project .ai/runtime ai trust init --name smoke --json >"$QUIET_LOG"
uv run --project .ai/runtime ai render --json >"$QUIET_LOG"
printf '{"reason":"smoke"}' | uv run --project .ai/runtime ai inbox request --gate remote_enable --summary smoke --json >"$APPROVAL_OUTPUT"
APPROVAL_ID="$(uv run --project .ai/runtime python -c 'import json,sys; print(json.load(open(sys.argv[1]))["approval"]["approval_id"])' "$APPROVAL_OUTPUT")"
uv run --project .ai/runtime ai inbox approve "$APPROVAL_ID" --json >"$QUIET_LOG"
printf '{"summary":"smoke"}' | uv run --project .ai/runtime ai notify enqueue --channel stdout --json >"$QUIET_LOG"
uv run --project .ai/runtime ai obs metrics --json >"$QUIET_LOG"
uv run --project .ai/runtime ai diagnostics bundle --dry-run --json >"$QUIET_LOG"
uv run --project .ai/runtime ai upgrade apply --target-version 0.1.1 --dry-run --json >"$QUIET_LOG"
uv run --project .ai/runtime ai index rebuild --json >"$QUIET_LOG"
uv run --project .ai/runtime ai report status --json >"$QUIET_LOG"
uv run --project .ai/runtime ai report release-notes >"$QUIET_LOG"
if ./scripts/package.sh >"$PACKAGE_OUTPUT" 2>"$PACKAGE_ERROR"; then
  echo "smoke failed: package script unexpectedly succeeded without git metadata" >&2
  exit 1
fi
if ! grep -Fq "package failed: git HEAD unavailable" "$PACKAGE_ERROR"; then
  cat "$PACKAGE_ERROR" >&2
  echo "smoke failed: package script did not report missing git metadata" >&2
  exit 1
fi

CI=true uv run --project .ai/runtime ai obs metrics --json >"$QUIET_LOG"
if CI=true uv run --project .ai/runtime ai render >"$QUIET_LOG" 2>&1; then
  echo "expected CI render write rejection" >&2
  exit 1
fi

echo "smoke ok"
