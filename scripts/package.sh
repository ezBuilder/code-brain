#!/usr/bin/env bash
set -euo pipefail
export COPYFILE_DISABLE=1

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="$("$ROOT/.ai/bin/ai" --json version | python -c 'import json,sys; print(json.load(sys.stdin)["version"])')"
OUT_DIR="$ROOT/dist"
NAME="code-brain-${VERSION}"
ARCHIVE="$OUT_DIR/${NAME}.tar.gz"
MANIFEST="$OUT_DIR/${NAME}.manifest.json"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$OUT_DIR"
rm -f "$ARCHIVE" "$ARCHIVE.sha256" "$MANIFEST"
mkdir -p "$TMP/$NAME"

tar \
  --exclude './.git' \
  --exclude './dist' \
  --exclude './.ai/cache' \
  --exclude './.ai/runtime/.venv' \
  --exclude './.ai/runtime/.pytest_cache' \
  --exclude './.ai/runtime/src/ai_core/__pycache__' \
  --exclude './.ai/runtime/src/ai_core/worker/__pycache__' \
  --exclude './.ai/runtime/tests/__pycache__' \
  -C "$ROOT" -cf - . | tar -C "$TMP/$NAME" -xf -

tar -C "$TMP" -czf "$ARCHIVE" "$NAME"

python - "$ARCHIVE" "$MANIFEST" "$VERSION" <<'PY'
import json
import hashlib
import pathlib
import sys
import tarfile

archive = pathlib.Path(sys.argv[1])
manifest_path = pathlib.Path(sys.argv[2])
version = sys.argv[3]
archive_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
archive.with_suffix(archive.suffix + ".sha256").write_text(f"{archive_digest}  {archive.name}\n", encoding="utf-8")

files = []
with tarfile.open(archive, "r:gz") as tar:
    for member in sorted((m for m in tar.getmembers() if m.isfile()), key=lambda item: item.name):
        extracted = tar.extractfile(member)
        if extracted is None:
            raise SystemExit(f"cannot read archive member: {member.name}")
        data = extracted.read()
        files.append(
            {
                "path": member.name,
                "mode": oct(member.mode),
                "size": member.size,
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )

manifest = {
    "schema_version": 1,
    "name": "code-brain",
    "version": version,
    "archive": archive.name,
    "archive_sha256": archive_digest,
    "file_count": len(files),
    "files": files,
}
manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

print(archive)
print(archive.with_suffix(archive.suffix + ".sha256"))
print(manifest_path)
PY
