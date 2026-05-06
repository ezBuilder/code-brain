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
SBOM="${ARCHIVE%.tar.gz}.sbom.json"
PROVENANCE="${ARCHIVE%.tar.gz}.provenance.json"

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

if [[ -f "$SBOM" ]]; then
  python - "$PKG_DIR" "$SBOM" <<'PY'
import hashlib
import json
import pathlib
import sys
import tomllib

pkg_dir = pathlib.Path(sys.argv[1])
sbom_path = pathlib.Path(sys.argv[2])
sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
lock_path = pkg_dir / ".ai" / "runtime" / "uv.lock"
if not lock_path.is_file():
    raise SystemExit("SBOM lockfile target missing")
if sbom.get("lockfile_sha256") != hashlib.sha256(lock_path.read_bytes()).hexdigest():
    raise SystemExit("SBOM lockfile sha256 mismatch")
lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
lock_packages = sorted(package["name"] for package in lock.get("package", []))
sbom_packages = sorted(package["name"] for package in sbom.get("packages", []))
if lock_packages != sbom_packages:
    raise SystemExit("SBOM package list mismatch")
if sbom.get("package_count") != len(sbom_packages):
    raise SystemExit("SBOM package_count mismatch")
PY
fi

if [[ -f "$PROVENANCE" ]]; then
  python - "$ARCHIVE" "$MANIFEST" "$SBOM" "$PROVENANCE" <<'PY'
import hashlib
import json
import pathlib
import sys

archive = pathlib.Path(sys.argv[1])
manifest = pathlib.Path(sys.argv[2])
sbom = pathlib.Path(sys.argv[3])
provenance_path = pathlib.Path(sys.argv[4])
provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
subjects = provenance.get("subjects", {})
required = [archive]
if manifest.is_file():
    required.append(manifest)
if sbom.is_file():
    required.append(sbom)
for path in required:
    expected = subjects.get(path.name)
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if expected != actual:
        raise SystemExit(f"provenance subject mismatch: {path.name}")
if provenance.get("git", {}).get("status_short") is None:
    raise SystemExit("provenance git status missing")
PY
fi

cd "$PKG_DIR"

uv run --project .ai/runtime ai --json version >/dev/null
uv run --project .ai/runtime ai doctor --strict --json >/dev/null
.ai/bin/ai --json version >/dev/null
.ai/bin/ai-hook SessionStart --json <<< '{"agent":"install-check"}' >/dev/null
POWERSHELL_BIN="$(command -v pwsh || command -v powershell || true)"
if [[ -n "$POWERSHELL_BIN" ]]; then
  "$POWERSHELL_BIN" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File .ai/bin/ai.ps1 --json version >/dev/null
  printf '{"agent":"install-check-pwsh"}' | "$POWERSHELL_BIN" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File .ai/bin/ai-hook.ps1 SessionStart --json >/dev/null
else
  echo "install check note: PowerShell not found; skipped ps1 shim execution" >&2
fi
uv run --project .ai/runtime python -m pytest .ai/runtime/tests >/dev/null

echo "install check ok: $ARCHIVE"
