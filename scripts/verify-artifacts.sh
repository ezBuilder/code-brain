#!/usr/bin/env bash
set -euo pipefail
export COPYFILE_DISABLE=1

ARCHIVE="${1:-}"

if [[ -z "$ARCHIVE" ]]; then
  echo "usage: $0 dist/code-brain-<version>.tar.gz" >&2
  exit 2
fi

if [[ ! -f "$ARCHIVE" ]]; then
  echo "archive not found: $ARCHIVE" >&2
  exit 2
fi

SHA_FILE="$ARCHIVE.sha256"
MANIFEST="${ARCHIVE%.tar.gz}.manifest.json"
SBOM="${ARCHIVE%.tar.gz}.sbom.json"
PROVENANCE="${ARCHIVE%.tar.gz}.provenance.json"
RELEASE_NOTES="${ARCHIVE%.tar.gz}.release-notes.md"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

python - "$ARCHIVE" "$SHA_FILE" "$MANIFEST" "$SBOM" "$PROVENANCE" "$RELEASE_NOTES" "$TMP" <<'PY'
import hashlib
import json
import pathlib
import sys
import tarfile
import tomllib

archive = pathlib.Path(sys.argv[1])
sha_file = pathlib.Path(sys.argv[2])
manifest_path = pathlib.Path(sys.argv[3])
sbom_path = pathlib.Path(sys.argv[4])
provenance_path = pathlib.Path(sys.argv[5])
release_notes_path = pathlib.Path(sys.argv[6])
tmp = pathlib.Path(sys.argv[7])


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: pathlib.Path) -> dict:
    if not path.is_file():
        raise SystemExit(f"artifact missing: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"artifact is not a JSON object: {path}")
    return payload


if not sha_file.is_file():
    raise SystemExit(f"artifact missing: {sha_file}")
expected_archive_sha = sha_file.read_text(encoding="utf-8").split()[0]
actual_archive_sha = sha256(archive)
if expected_archive_sha != actual_archive_sha:
    raise SystemExit(f"checksum mismatch: expected {expected_archive_sha}, got {actual_archive_sha}")

with tarfile.open(archive, "r:gz") as tar:
    members = tar.getmembers()
    roots = {pathlib.PurePosixPath(member.name).parts[0] for member in members if member.name and pathlib.PurePosixPath(member.name).parts}
    if len(roots) != 1:
        raise SystemExit(f"archive must contain exactly one package root, got: {sorted(roots)}")
    tar.extractall(tmp)

pkg_name = next(iter(roots))
pkg_dir = tmp / pkg_name
if not pkg_dir.is_dir():
    raise SystemExit(f"package root missing after extract: {pkg_name}")

manifest = load_json(manifest_path)
if manifest.get("archive") != archive.name:
    raise SystemExit("manifest archive name mismatch")
if manifest.get("archive_sha256") != actual_archive_sha:
    raise SystemExit("manifest archive sha256 mismatch")
files = manifest.get("files")
if not isinstance(files, list) or manifest.get("file_count") != len(files):
    raise SystemExit("manifest file_count mismatch")
for item in files:
    path = pathlib.PurePosixPath(item["path"])
    try:
        relative = path.relative_to(pkg_name)
    except ValueError as exc:
        raise SystemExit(f"manifest path outside package root: {path}") from exc
    target = pkg_dir / pathlib.Path(relative)
    if not target.is_file():
        raise SystemExit(f"manifest file missing after extract: {path}")
    if sha256(target) != item["sha256"]:
        raise SystemExit(f"manifest file checksum mismatch: {path}")
    if target.stat().st_size != item["size"]:
        raise SystemExit(f"manifest file size mismatch: {path}")

sbom = load_json(sbom_path)
lock_path = pkg_dir / ".ai" / "runtime" / "uv.lock"
if not lock_path.is_file():
    raise SystemExit("SBOM lockfile target missing")
if sbom.get("lockfile_sha256") != sha256(lock_path):
    raise SystemExit("SBOM lockfile sha256 mismatch")
lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
lock_packages = sorted(package["name"] for package in lock.get("package", []))
sbom_packages = sorted(package["name"] for package in sbom.get("packages", []))
if lock_packages != sbom_packages:
    raise SystemExit("SBOM package list mismatch")
if sbom.get("package_count") != len(sbom_packages):
    raise SystemExit("SBOM package_count mismatch")

provenance = load_json(provenance_path)
if not release_notes_path.is_file():
    raise SystemExit(f"artifact missing: {release_notes_path}")
subjects = provenance.get("subjects", {})
if not isinstance(subjects, dict):
    raise SystemExit("provenance subjects missing")
required_subjects = {
    archive.name: actual_archive_sha,
    manifest_path.name: sha256(manifest_path),
    sbom_path.name: sha256(sbom_path),
    release_notes_path.name: sha256(release_notes_path),
}
for name, digest in required_subjects.items():
    if subjects.get(name) != digest:
        raise SystemExit(f"provenance subject mismatch: {name}")
if provenance.get("git", {}).get("status_short") is None:
    raise SystemExit("provenance git status missing")

release_notes = release_notes_path.read_text(encoding="utf-8")
required_notes = [
    f"# Code Brain {manifest.get('version')} Release Notes",
    actual_archive_sha,
    manifest_path.name,
    sbom_path.name,
    provenance_path.name,
    "./scripts/release-gate.sh",
]
for needle in required_notes:
    if needle not in release_notes:
        raise SystemExit(f"release notes missing required text: {needle}")

print(
    json.dumps(
        {
            "ok": True,
            "archive": archive.name,
            "archive_sha256": actual_archive_sha,
            "manifest_file_count": len(files),
            "release_notes_sha256": sha256(release_notes_path),
            "sbom_package_count": len(sbom_packages),
            "provenance_git_head": provenance.get("git", {}).get("head"),
        },
        sort_keys=True,
    )
)
PY
