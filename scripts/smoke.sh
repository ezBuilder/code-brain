#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

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

uv run --project .ai/runtime python -m pytest .ai/runtime/tests
uv run --project .ai/runtime ai doctor --strict --json >/dev/null
uv run --project .ai/runtime ai render --dry-run --json >/dev/null
uv run --project .ai/runtime ai index rebuild --json >/dev/null
uv run --project .ai/runtime ai code query worker --json >/dev/null
printf '{"agent":"codex"}' | uv run --project .ai/runtime ai hook SessionStart --json >/dev/null
printf '{"task":"smoke"}' | uv run --project .ai/runtime ai queue enqueue --priority P2 --kind smoke --json >/tmp/code-brain-smoke-job.json
JOB_ID="$(python -c 'import json; print(json.load(open("/tmp/code-brain-smoke-job.json"))["job"]["id"])')"
uv run --project .ai/runtime ai queue lease --worker-id smoke --json >/tmp/code-brain-smoke-lease.json
LEASE_ID="$(python -c 'import json; print(json.load(open("/tmp/code-brain-smoke-lease.json"))["job"]["lease_id"])')"
uv run --project .ai/runtime ai queue complete --job-id "$JOB_ID" --lease-id "$LEASE_ID" --json >/dev/null
uv run --project .ai/runtime ai trust init --name smoke --json >/dev/null
uv run --project .ai/runtime ai render --json >/dev/null
printf '{"reason":"smoke"}' | uv run --project .ai/runtime ai inbox request --gate remote_enable --summary smoke --json >/tmp/code-brain-smoke-approval.json
APPROVAL_ID="$(python -c 'import json; print(json.load(open("/tmp/code-brain-smoke-approval.json"))["approval"]["approval_id"])')"
uv run --project .ai/runtime ai inbox approve "$APPROVAL_ID" --json >/dev/null
printf '{"summary":"smoke"}' | uv run --project .ai/runtime ai notify enqueue --channel stdout --json >/dev/null
uv run --project .ai/runtime ai obs metrics --json >/dev/null
uv run --project .ai/runtime ai diagnostics bundle --dry-run --json >/dev/null
uv run --project .ai/runtime ai upgrade apply --target-version 0.1.1 --dry-run --json >/dev/null

CI=true uv run --project .ai/runtime ai obs metrics --json >/dev/null
if CI=true uv run --project .ai/runtime ai render >/dev/null 2>&1; then
  echo "expected CI render write rejection" >&2
  exit 1
fi

echo "smoke ok"
