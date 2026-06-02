#!/usr/bin/env bash
set -euo pipefail
export COPYFILE_DISABLE=1

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

COPY="$TMP/code-brain"
mkdir -p "$COPY"
cd "$ROOT"
tar \
  --exclude './.git' \
  --exclude './.ai/cache' \
  --exclude './.ai/runtime/.venv' \
  --exclude './.ai/runtime/.pytest_cache' \
  --exclude './.pytest_cache' \
  --exclude './__pycache__' \
  --exclude './dist' \
  -cf - . | tar -xf - -C "$COPY"

cd "$COPY"
git init -q
git config user.email "bootstrap-idempotency@example.invalid"
git config user.name "Bootstrap Idempotency"
git add -A
git commit -qm bootstrap-idempotency-baseline

manifest_sha() {
  shasum -a 256 .ai/generated/manifest.json | awk '{print $1}'
}

BASE_MANIFEST_SHA="$(manifest_sha)"

run_bootstrap() {
  local label="$1"
  CI=true GITHUB_ACTIONS=true ./bootstrap.sh >/dev/null
  local status
  status="$(git status --short)"
  if [[ -n "$status" ]]; then
    printf '%s\n' "$status"
    echo "bootstrap idempotency failed after $label: tracked working tree changed" >&2
    exit 1
  fi
  local current_manifest_sha
  current_manifest_sha="$(manifest_sha)"
  if [[ "$current_manifest_sha" != "$BASE_MANIFEST_SHA" ]]; then
    echo "bootstrap idempotency failed after $label: generated manifest changed" >&2
    echo "before: $BASE_MANIFEST_SHA" >&2
    echo "after:  $current_manifest_sha" >&2
    exit 1
  fi
}

run_bootstrap first
run_bootstrap second

echo "bootstrap idempotency ok"
