#!/usr/bin/env bash
set -euo pipefail
export COPYFILE_DISABLE=1

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
py() {
  if [[ -x "$ROOT/.ai/runtime/.venv/bin/python" ]]; then
    "$ROOT/.ai/runtime/.venv/bin/python" "$@"
  elif command -v uv >/dev/null 2>&1; then
    uv run --project "$ROOT/.ai/runtime" python "$@"
  else
    local _py
    _py="$(command -v python3 || command -v python || true)"
    if [[ -z "$_py" ]]; then
      echo "rollback drill failed: no python3/python interpreter found on PATH" >&2
      exit 2
    fi
    "$_py" "$@"
  fi
}
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

WORK="$TMP/code-brain"
mkdir -p "$WORK"

tar \
  --exclude './.git' \
  --exclude './dist' \
  --exclude './.DS_Store' \
  --exclude './*/.DS_Store' \
  --exclude './__MACOSX' \
  --exclude './*/__MACOSX' \
  --exclude './._*' \
  --exclude './*/._*' \
  --exclude './.ai/cache' \
  --exclude './.ai/runtime/.venv' \
  --exclude './.ai/runtime/.pytest_cache' \
  --exclude './.ai/runtime/src/ai_core/__pycache__' \
  --exclude './.ai/runtime/src/ai_core/worker/__pycache__' \
  --exclude './.ai/runtime/tests/__pycache__' \
  -C "$ROOT" -cf - . | tar -C "$WORK" -xf -

cd "$WORK"
unset CI GITHUB_ACTIONS GITLAB_CI AI_CI

manifest=".ai/generated/manifest.json"
before="$(shasum -a 256 "$manifest" | awk '{print $1}')"

dry_run_json="$(uv run --project .ai/runtime ai upgrade apply --target-version 0.1.1 --dry-run --json)"
py - "$dry_run_json" "$WORK" <<'PY'
import json
import pathlib
import sys

payload = json.loads(sys.argv[1])
root = pathlib.Path(sys.argv[2])
if payload.get("dry_run") is not True:
    raise SystemExit("rollback drill failed: dry-run flag missing")
backup_path = payload.get("backup_path")
if not isinstance(backup_path, str):
    raise SystemExit("rollback drill failed: dry-run backup path missing")
if (root / backup_path).exists():
    raise SystemExit("rollback drill failed: dry-run created a backup")
PY

after_dry="$(shasum -a 256 "$manifest" | awk '{print $1}')"
if [[ "$before" != "$after_dry" ]]; then
  echo "rollback drill failed: dry-run mutated manifest" >&2
  exit 1
fi

apply_json="$(uv run --project .ai/runtime ai upgrade apply --target-version 0.1.1 --json)"
backup_path="$(py - "$apply_json" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
if payload.get("ok") is not True or payload.get("dry_run") is not False:
    raise SystemExit("rollback drill failed: apply did not succeed")
print(payload["backup_path"])
PY
)"

if [[ ! -f "$backup_path" ]]; then
  echo "rollback drill failed: rollback backup was not created" >&2
  exit 1
fi

py - "$manifest" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
path.write_text(path.read_text(encoding="utf-8") + "\n{\"drift\": true}\n", encoding="utf-8")
PY

drifted="$(shasum -a 256 "$manifest" | awk '{print $1}')"
if [[ "$before" == "$drifted" ]]; then
  echo "rollback drill failed: drift simulation did not change manifest" >&2
  exit 1
fi

uv run --project .ai/runtime ai upgrade rollback --backup-path "$backup_path" --json >/dev/null

after_rollback="$(shasum -a 256 "$manifest" | awk '{print $1}')"
if [[ "$before" != "$after_rollback" ]]; then
  echo "rollback drill failed: rollback did not restore manifest byte-for-byte" >&2
  exit 1
fi

uv run --project .ai/runtime ai doctor --strict --json >/dev/null

echo "rollback drill ok"
