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

./scripts/env-check.sh >/dev/null
./scripts/preflight.sh --check-only >/dev/null
./scripts/lockfile-check.sh >/dev/null
./scripts/lint.sh
# The gate owns the full suite explicitly (sharded, ~2m instead of ~8m serial) and
# then runs bootstrap for its render/doctor/venv contract only. Previously
# bootstrap re-ran the SAME 2,615 tests serially inside the gate, which was the
# single largest cost of a release. AI_BOOTSTRAP_SKIP_TESTS is bootstrap's own
# documented switch; the suite is not skipped, it moved one line up.
uv run --project .ai/runtime python scripts/test-sharded.py
AI_BOOTSTRAP_SKIP_TESTS=1 ./bootstrap.sh
./scripts/smoke.sh
./scripts/docs-check.sh
./scripts/package.sh >"$PACKAGE_OUTPUT"
ARCHIVE="$(head -n 1 "$PACKAGE_OUTPUT")"
if [[ -z "$ARCHIVE" || ! -f "$ARCHIVE" ]]; then
  cat "$PACKAGE_OUTPUT" >&2
  echo "release gate failed: package script did not emit an archive path" >&2
  exit 1
fi
CURRENT_VERSION="$("$ROOT/.ai/bin/ai" --json version | py -c 'import json,sys; print(json.load(sys.stdin)["version"])')"
py - "$ROOT/dist" "$CURRENT_VERSION" <<'PY' >/dev/null
import sys
from pathlib import Path
from ai_core.report import release_retention_plan

plan = release_retention_plan(Path(sys.argv[1]), sys.argv[2])
if not plan["clean"]:
    raise SystemExit("release gate failed: stale Code Brain release artifacts remain")
PY
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
py - "$REPORT_OUTPUT" <<'PY'
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
