#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PACKAGE_OUTPUT="$(mktemp)"
REPORT_OUTPUT="$(mktemp)"
trap 'rm -f "$PACKAGE_OUTPUT" "$REPORT_OUTPUT"' EXIT

./scripts/env-check.sh >/dev/null
./scripts/preflight.sh --check-only >/dev/null
uv lock --check --project .ai/runtime >/dev/null
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
./scripts/reproducibility-check.sh "$ARCHIVE" >/dev/null
./scripts/artifact-tamper-check.sh "$ARCHIVE"
./scripts/rollback-drill.sh >/dev/null
./scripts/bootstrap-idempotency.sh >/dev/null
./scripts/dep-advisory.sh >/dev/null
uv run --project .ai/runtime ai doctor --strict --json >/dev/null
uv run --project .ai/runtime ai report status --json >"$REPORT_OUTPUT"
mkdir -p dist
uv run --project .ai/runtime ai report release-gate-summary --git-sha "$(git rev-parse HEAD)" --json >dist/release-gate.summary.json
python - "$REPORT_OUTPUT" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
artifacts = payload.get("release_artifacts", {})
if payload.get("release_ready") is not True:
    print("release gate failed: release_ready is not true", file=sys.stderr)
    print(json.dumps({"release_ready": payload.get("release_ready"), "release_artifacts": artifacts}, indent=2), file=sys.stderr)
    raise SystemExit(1)
if artifacts.get("all_current") is not True:
    print("release gate failed: release artifacts are not current", file=sys.stderr)
    print(json.dumps(artifacts, indent=2), file=sys.stderr)
    raise SystemExit(1)
PY

if [[ -n "$(git status --short)" ]]; then
  git status --short
  echo "release gate failed: tracked working tree is dirty" >&2
  exit 1
fi

echo "release gate ok"
