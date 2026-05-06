#!/usr/bin/env bash
set -euo pipefail
export COPYFILE_DISABLE=1

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="$("$ROOT/.ai/bin/ai" --json version | python -c 'import json,sys; print(json.load(sys.stdin)["version"])')"
OUT_DIR="$ROOT/dist"
NAME="code-brain-${VERSION}"
ARCHIVE="$OUT_DIR/${NAME}.tar.gz"
MANIFEST="$OUT_DIR/${NAME}.manifest.json"
SBOM="$OUT_DIR/${NAME}.sbom.json"
PROVENANCE="$OUT_DIR/${NAME}.provenance.json"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$OUT_DIR"
rm -f "$ARCHIVE" "$ARCHIVE.sha256" "$MANIFEST" "$SBOM" "$PROVENANCE"
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

python - "$ROOT" "$ARCHIVE" "$MANIFEST" "$SBOM" "$PROVENANCE" "$VERSION" <<'PY'
import json
import hashlib
import pathlib
import subprocess
import sys
import tarfile
import tomllib
from datetime import datetime, timezone

root = pathlib.Path(sys.argv[1])
archive = pathlib.Path(sys.argv[2])
manifest_path = pathlib.Path(sys.argv[3])
sbom_path = pathlib.Path(sys.argv[4])
provenance_path = pathlib.Path(sys.argv[5])
version = sys.argv[6]
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
manifest_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

lock_path = root / ".ai" / "runtime" / "uv.lock"
lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
packages = []
for package in lock.get("package", []):
    entry = {
        "name": package["name"],
        "version": package["version"],
        "source": package.get("source", {}),
    }
    hashes = []
    sdist = package.get("sdist")
    if isinstance(sdist, dict) and sdist.get("hash"):
        hashes.append({"type": "sdist", "hash": sdist["hash"], "url": sdist.get("url")})
    for wheel in package.get("wheels", []):
        if wheel.get("hash"):
            hashes.append({"type": "wheel", "hash": wheel["hash"], "url": wheel.get("url")})
    if hashes:
        entry["hashes"] = hashes
    dependencies = package.get("dependencies", [])
    if dependencies:
        entry["dependencies"] = dependencies
    packages.append(entry)

sbom = {
    "schema_version": 1,
    "format": "code-brain-runtime-sbom",
    "name": "code-brain",
    "version": version,
    "lockfile": ".ai/runtime/uv.lock",
    "lockfile_sha256": hashlib.sha256(lock_path.read_bytes()).hexdigest(),
    "package_count": len(packages),
    "packages": sorted(packages, key=lambda item: item["name"]),
}
sbom_path.write_text(json.dumps(sbom, indent=2, sort_keys=True) + "\n", encoding="utf-8")
sbom_digest = hashlib.sha256(sbom_path.read_bytes()).hexdigest()

def git_output(*args: str) -> str | None:
    try:
        return subprocess.check_output(["git", *args], cwd=root, text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None

provenance = {
    "schema_version": 1,
    "name": "code-brain",
    "version": version,
    "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    "builder": {
        "script": "scripts/package.sh",
        "python": sys.version.split()[0],
    },
    "git": {
        "branch": git_output("branch", "--show-current"),
        "head": git_output("rev-parse", "--short=12", "HEAD"),
        "status_short": git_output("status", "--short") or "",
    },
    "subjects": {
        archive.name: archive_digest,
        manifest_path.name: manifest_digest,
        sbom_path.name: sbom_digest,
    },
}
provenance_path.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")

print(archive)
print(archive.with_suffix(archive.suffix + ".sha256"))
print(manifest_path)
print(sbom_path)
print(provenance_path)
PY
