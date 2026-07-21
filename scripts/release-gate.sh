#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
py() {
  if [[ -x "$ROOT/.ai/runtime/.venv/bin/python" ]]; then
    "$ROOT/.ai/runtime/.venv/bin/python" "$@"
  elif command -v uv >/dev/null 2>&1; then
    uv run --project "$ROOT/.ai/runtime" python "$@"
  else
    local _py
    _py="$(command -v python3 || command -v python || true)"
    if [[ -z "$_py" ]]; then
      echo "release gate failed: no python3/python interpreter found on PATH" >&2
      exit 2
    fi
    "$_py" "$@"
  fi
}
PACKAGE_OUTPUT="$(mktemp)"
REPORT_OUTPUT="$(mktemp)"
trap 'rm -f "$PACKAGE_OUTPUT" "$REPORT_OUTPUT"' EXIT
RUNNER_EVIDENCE_TOKEN="$(py - <<'PY'
import secrets

print(secrets.token_urlsafe(32))
PY
)"

./scripts/env-check.sh >/dev/null
./scripts/preflight.sh --check-only >/dev/null
./scripts/lockfile-check.sh >/dev/null
./scripts/lint.sh
AI_RUNNER_EVIDENCE_TOKEN="$RUNNER_EVIDENCE_TOKEN" ./bootstrap.sh
./scripts/smoke.sh
./scripts/docs-check.sh
uv run --project .ai/runtime python scripts/stress-bounds.py --files 2000 --iterations 5 >/dev/null
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
RUNNER_EVIDENCE_TOKEN="$RUNNER_EVIDENCE_TOKEN" py - "$REPORT_OUTPUT" <<'PY'
import hashlib
import json
import os
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
artifacts = payload.get("release_artifacts", {})
operational = payload.get("operational_bounds", {})
if payload.get("release_ready") is not True:
    print("release gate failed: release_ready is not true", file=sys.stderr)
    print(json.dumps({"release_ready": payload.get("release_ready"), "release_artifacts": artifacts, "operational_bounds": operational}, indent=2), file=sys.stderr)
    raise SystemExit(1)
if operational.get("ok") is not True:
    print("release gate failed: operational bounds are not healthy", file=sys.stderr)
    print(json.dumps(operational, indent=2), file=sys.stderr)
    raise SystemExit(1)
runner = operational.get("runner", {})
if runner.get("observed") is not True:
    print("release gate failed: observed test-runner evidence is missing", file=sys.stderr)
    print(json.dumps(runner, indent=2), file=sys.stderr)
    raise SystemExit(1)
expected_evidence = hashlib.sha256(os.environ["RUNNER_EVIDENCE_TOKEN"].encode("utf-8")).hexdigest()
if (
    runner.get("ok") is not True
    or runner.get("label") != "bootstrap-pytest"
    or runner.get("evidence_token_sha256") != expected_evidence
    or runner.get("interrupt_observation_enabled") is not True
):
    print("release gate failed: runner evidence is stale, unbound, or interrupt-unsafe", file=sys.stderr)
    print(json.dumps(runner, indent=2), file=sys.stderr)
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
