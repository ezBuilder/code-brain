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
      echo "package failed: no python3/python interpreter found on PATH" >&2
      exit 2
    fi
    "$_py" "$@"
  fi
}
VERSION="$("$ROOT/.ai/bin/ai" --json version | py -c 'import json,sys; print(json.load(sys.stdin)["version"])')"
OUT_DIR="${DIST_OVERRIDE:-$ROOT/dist}"
NAME="code-brain-${VERSION}"
ARCHIVE="$OUT_DIR/${NAME}.tar.gz"
MANIFEST="$OUT_DIR/${NAME}.manifest.json"
SBOM="$OUT_DIR/${NAME}.sbom.json"
PROVENANCE="$OUT_DIR/${NAME}.provenance.json"
RELEASE_NOTES="$OUT_DIR/${NAME}.release-notes.md"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

GIT_HEAD="$(git -C "$ROOT" rev-parse --short=12 HEAD 2>/dev/null || true)"
if [[ -z "$GIT_HEAD" ]]; then
  echo "package failed: git HEAD unavailable" >&2
  exit 1
fi
GIT_STATUS="$(git -C "$ROOT" status --short)"
if [[ -n "$GIT_STATUS" ]]; then
  echo "package failed: tracked working tree is dirty" >&2
  git -C "$ROOT" status --short >&2
  exit 1
fi

mkdir -p "$OUT_DIR"
rm -f "$ARCHIVE" "$ARCHIVE.sha256" "$MANIFEST" "$SBOM" "$PROVENANCE" "$RELEASE_NOTES"
COMMIT_TIME="$(git -C "$ROOT" log -1 --format=%ct)"

py - "$ROOT" "$ARCHIVE" "$MANIFEST" "$SBOM" "$PROVENANCE" "$RELEASE_NOTES" "$VERSION" "$NAME" "$COMMIT_TIME" <<'PY'
import json
import hashlib
import gzip
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
release_notes_path = pathlib.Path(sys.argv[6])
version = sys.argv[7]
package_root = sys.argv[8]
commit_time = int(sys.argv[9])


def excluded(path: pathlib.Path) -> bool:
    parts = path.parts
    if not parts:
        return False
    if parts[0] in {".git", "dist"}:
        return True
    if ".DS_Store" in parts or "__MACOSX" in parts or "__pycache__" in parts:
        return True
    if any(part.startswith("._") for part in parts):
        return True
    if parts[:2] == (".ai", "cache"):
        return True
    if parts[:3] == (".ai", "runtime", ".venv"):
        return True
    if parts[:3] == (".ai", "runtime", ".pytest_cache"):
        return True
    return False


def archive_name(path: pathlib.Path) -> str:
    return pathlib.PurePosixPath(package_root, path.as_posix()).as_posix()


def normalized_info(path: pathlib.Path, arcname: str) -> tarfile.TarInfo:
    source = root / path
    info = tarfile.TarInfo(arcname)
    info.mtime = commit_time
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    if source.is_dir():
        info.type = tarfile.DIRTYPE
        info.mode = 0o755
        info.size = 0
    elif source.is_file():
        info.type = tarfile.REGTYPE
        info.mode = 0o755 if source.stat().st_mode & 0o111 else 0o644
        info.size = source.stat().st_size
    else:
        raise SystemExit(f"unsupported archive member type: {path.as_posix()}")
    return info


def build_archive() -> None:
    paths = [path.relative_to(root) for path in root.rglob("*") if not excluded(path.relative_to(root))]
    members = sorted(paths, key=lambda item: item.as_posix())
    with archive.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
                root_info = tarfile.TarInfo(package_root)
                root_info.type = tarfile.DIRTYPE
                root_info.mode = 0o755
                root_info.mtime = commit_time
                root_info.uid = 0
                root_info.gid = 0
                root_info.uname = ""
                root_info.gname = ""
                tar.addfile(root_info)
                for path in members:
                    if (root / path).is_dir():
                        tar.addfile(normalized_info(path, archive_name(path)))
                    else:
                        with (root / path).open("rb") as handle:
                            tar.addfile(normalized_info(path, archive_name(path)), handle)


build_archive()
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

git_branch = git_output("branch", "--show-current")
git_head = git_output("rev-parse", "--short=12", "HEAD")
git_status = git_output("status", "--short") or ""
commits = git_output("log", "--oneline", "--decorate", "-12") or ""

release_notes = "\n".join(
    [
        f"# Code Brain {version} Release Notes",
        "",
        "## Status",
        "",
        f"- Runtime version: `{version}`",
        "- Protocol version: `1`",
        f"- Git HEAD: `{git_head or ''}`",
        f"- Git status: `{'clean' if not git_status else 'dirty'}`",
        f"- Archive: `{archive.name}`",
        f"- Archive SHA-256: `{archive_digest}`",
        f"- Manifest: `{manifest_path.name}`",
        f"- Manifest SHA-256: `{manifest_digest}`",
        f"- SBOM: `{sbom_path.name}`",
        f"- SBOM SHA-256: `{sbom_digest}`",
        f"- Provenance: `{provenance_path.name}`",
        "",
        "## Recent Commits",
        "",
        "```text",
        commits,
        "```",
        "",
        "## Verification",
        "",
        "```bash",
        "./scripts/env-check.sh",
        "./scripts/lint.sh",
        "./bootstrap.sh",
        "./scripts/smoke.sh",
        "./scripts/docs-check.sh",
        "./scripts/package.sh",
        f"./scripts/verify-artifacts.sh dist/code-brain-{version}.tar.gz",
        f"./scripts/install-check.sh dist/code-brain-{version}.tar.gz",
        f"./scripts/artifact-tamper-check.sh dist/code-brain-{version}.tar.gz",
        "./scripts/release-gate.sh",
        "uv run --project .ai/runtime ai doctor --strict --json",
        "uv run --project .ai/runtime ai report status --json",
        "git status --short",
        "```",
        "",
    ]
)
release_notes_path.write_text(release_notes, encoding="utf-8")
release_notes_digest = hashlib.sha256(release_notes_path.read_bytes()).hexdigest()

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
        "branch": git_branch,
        "head": git_head,
        "status_short": git_status,
    },
    "subjects": {
        archive.name: archive_digest,
        manifest_path.name: manifest_digest,
        sbom_path.name: sbom_digest,
        release_notes_path.name: release_notes_digest,
    },
}
provenance_path.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")

print(archive)
print(archive.with_suffix(archive.suffix + ".sha256"))
print(manifest_path)
print(sbom_path)
print(provenance_path)
print(release_notes_path)
PY
