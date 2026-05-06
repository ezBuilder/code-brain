#!/usr/bin/env bash
set -euo pipefail
export COPYFILE_DISABLE=1

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ARCHIVE="${1:-}"

if [[ -z "$ARCHIVE" ]]; then
  ARCHIVE="$(ls -t "$ROOT"/dist/code-brain-*.tar.gz | head -n 1)"
fi

if [[ ! -f "$ARCHIVE" ]]; then
  echo "archive not found: $ARCHIVE" >&2
  exit 2
fi

SHA_FILE="$ARCHIVE.sha256"
MANIFEST="${ARCHIVE%.tar.gz}.manifest.json"

if [[ -f "$SHA_FILE" ]]; then
  python - "$ARCHIVE" "$SHA_FILE" <<'PY'
import hashlib
import pathlib
import sys

archive = pathlib.Path(sys.argv[1])
sha_file = pathlib.Path(sys.argv[2])
expected = sha_file.read_text(encoding="utf-8").split()[0]
actual = hashlib.sha256(archive.read_bytes()).hexdigest()
if actual != expected:
    raise SystemExit(f"checksum mismatch: expected {expected}, got {actual}")
PY
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

tar -C "$TMP" -xzf "$ARCHIVE"
PKG_DIR="$(find "$TMP" -maxdepth 1 -type d -name 'code-brain-*' | head -n 1)"

if [[ -z "$PKG_DIR" ]]; then
  echo "package directory not found in archive" >&2
  exit 2
fi

if [[ -f "$MANIFEST" ]]; then
  python - "$PKG_DIR" "$MANIFEST" "$ARCHIVE" <<'PY'
import hashlib
import json
import pathlib
import sys

pkg_dir = pathlib.Path(sys.argv[1])
manifest_path = pathlib.Path(sys.argv[2])
archive = pathlib.Path(sys.argv[3])
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

if manifest.get("archive") != archive.name:
    raise SystemExit("manifest archive name mismatch")

archive_sha = hashlib.sha256(archive.read_bytes()).hexdigest()
if manifest.get("archive_sha256") != archive_sha:
    raise SystemExit("manifest archive sha256 mismatch")

for item in manifest.get("files", []):
    path = pathlib.Path(item["path"])
    try:
        relative = path.relative_to(pkg_dir.name)
    except ValueError as exc:
        raise SystemExit(f"manifest path outside package root: {path}") from exc
    target = pkg_dir / relative
    if not target.is_file():
        raise SystemExit(f"manifest file missing after extract: {path}")
    data = target.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != item["sha256"]:
        raise SystemExit(f"manifest file checksum mismatch: {path}")
    if target.stat().st_size != item["size"]:
        raise SystemExit(f"manifest file size mismatch: {path}")

if len(manifest.get("files", [])) != manifest.get("file_count"):
    raise SystemExit("manifest file_count mismatch")
PY
fi

cd "$PKG_DIR"

uv run --project .ai/runtime ai --json version >/dev/null
uv run --project .ai/runtime ai doctor --strict --json >/dev/null
.ai/bin/ai --json version >/dev/null
.ai/bin/ai-hook SessionStart --json <<< '{"agent":"install-check"}' >/dev/null
uv run --project .ai/runtime python -m pytest .ai/runtime/tests >/dev/null

echo "install check ok: $ARCHIVE"
