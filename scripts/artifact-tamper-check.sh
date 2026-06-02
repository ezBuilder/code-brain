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
      echo "artifact tamper check failed: no python3/python interpreter found on PATH" >&2
      exit 2
    fi
    "$_py" "$@"
  fi
}
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

ARCHIVE="${1:-}"
if [[ -z "$ARCHIVE" ]]; then
  ARCHIVE="$(ls -t "$ROOT"/dist/code-brain-*.tar.gz | head -n 1)"
fi

if [[ ! -f "$ARCHIVE" ]]; then
  echo "archive not found: $ARCHIVE" >&2
  exit 2
fi

BASE="$(basename "$ARCHIVE")"
PREFIX="${BASE%.tar.gz}"

copy_artifacts() {
  local target="$1"
  mkdir -p "$target/dist"
  cp "$ARCHIVE" "$target/dist/$BASE"
  for suffix in ".tar.gz.sha256" ".manifest.json" ".sbom.json" ".provenance.json" ".release-notes.md"; do
    local source
    if [[ "$suffix" == ".tar.gz.sha256" ]]; then
      source="$ARCHIVE.sha256"
    else
      source="$(dirname "$ARCHIVE")/$PREFIX$suffix"
    fi
    if [[ -f "$source" ]]; then
      cp "$source" "$target/dist/$(basename "$source")"
    fi
  done
}

expect_install_check_failure() {
  local name="$1"
  local dir="$TMP/$name"
  copy_artifacts "$dir"
  shift
  "$@" "$dir"
  local stdout="$TMP/tamper-$name.out"
  local stderr="$TMP/tamper-$name.err"
  if "$ROOT/scripts/verify-artifacts.sh" "$dir/dist/$BASE" >"$stdout" 2>"$stderr"; then
    echo "expected artifact verifier failure for tamper case: $name" >&2
    cat "$stdout" >&2
    exit 1
  fi
}

tamper_checksum() {
  local dir="$1"
  py - "$dir/dist/$BASE.sha256" <<'PY'
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
text = path.read_text(encoding="utf-8").split(maxsplit=1)
path.write_text("0" * 64 + "  " + text[1], encoding="utf-8")
PY
}

tamper_missing_checksum() {
  local dir="$1"
  rm -f "$dir/dist/$BASE.sha256"
}

tamper_manifest() {
  local dir="$1"
  py - "$dir/dist/$PREFIX.manifest.json" <<'PY'
import json
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
payload["files"][0]["sha256"] = "0" * 64
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

tamper_missing_manifest() {
  local dir="$1"
  rm -f "$dir/dist/$PREFIX.manifest.json"
}

tamper_sbom() {
  local dir="$1"
  py - "$dir/dist/$PREFIX.sbom.json" <<'PY'
import json
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
payload["lockfile_sha256"] = "0" * 64
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

tamper_missing_sbom() {
  local dir="$1"
  rm -f "$dir/dist/$PREFIX.sbom.json"
}

tamper_provenance() {
  local dir="$1"
  py - "$dir/dist/$PREFIX.provenance.json" <<'PY'
import json
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
subjects = payload.setdefault("subjects", {})
for key in list(subjects):
    if key.endswith(".tar.gz"):
        subjects[key] = "0" * 64
        break
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

tamper_missing_provenance() {
  local dir="$1"
  rm -f "$dir/dist/$PREFIX.provenance.json"
}

tamper_dirty_provenance() {
  local dir="$1"
  py - "$dir/dist/$PREFIX.provenance.json" <<'PY'
import json
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
payload.setdefault("git", {})["status_short"] = " M RELEASE.md"
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

tamper_metadata_version() {
  local dir="$1"
  py - "$dir/dist/$PREFIX.sbom.json" <<'PY'
import json
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
payload["version"] = "9.9.9"
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

tamper_missing_release_notes() {
  local dir="$1"
  rm -f "$dir/dist/$PREFIX.release-notes.md"
}

tamper_release_notes() {
  local dir="$1"
  py - "$dir/dist/$PREFIX.release-notes.md" <<'PY'
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
path.write_text(text.replace("Release Notes", "Release Log", 1), encoding="utf-8")
PY
}

tamper_release_notes_git_head() {
  local dir="$1"
  py - "$dir/dist/$PREFIX.release-notes.md" <<'PY'
import re
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
path.write_text(re.sub(r"- Git HEAD: `[^`]+`", "- Git HEAD: `000000000000`", text, count=1), encoding="utf-8")
PY
}

tamper_release_notes_git_status() {
  local dir="$1"
  py - "$dir/dist/$PREFIX.release-notes.md" <<'PY'
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
path.write_text(text.replace("- Git status: `clean`", "- Git status: `dirty`", 1), encoding="utf-8")
PY
}

expect_unsafe_archive_failure() {
  local dir="$TMP/unsafe_archive"
  local payload="$TMP/unsafe-payload"
  mkdir -p "$dir/dist" "$payload/$PREFIX"
  printf 'ok\n' >"$payload/$PREFIX/README.txt"
  printf 'bad\n' >"$payload/../escape.txt"
  py - "$dir/dist/$BASE" "$payload" "$PREFIX" <<'PY'
import pathlib
import sys
import tarfile

archive = pathlib.Path(sys.argv[1])
payload = pathlib.Path(sys.argv[2])
prefix = sys.argv[3]
with tarfile.open(archive, "w:gz") as tar:
    tar.add(payload / prefix / "README.txt", arcname=f"{prefix}/README.txt")
    tar.add(payload / "../escape.txt", arcname=f"{prefix}/../escape.txt")
PY
  cp "$ARCHIVE.sha256" "$dir/dist/$BASE.sha256"
  cp "$(dirname "$ARCHIVE")/$PREFIX.manifest.json" "$dir/dist/$PREFIX.manifest.json"
  cp "$(dirname "$ARCHIVE")/$PREFIX.sbom.json" "$dir/dist/$PREFIX.sbom.json"
  cp "$(dirname "$ARCHIVE")/$PREFIX.provenance.json" "$dir/dist/$PREFIX.provenance.json"
  cp "$(dirname "$ARCHIVE")/$PREFIX.release-notes.md" "$dir/dist/$PREFIX.release-notes.md"
  py - "$dir/dist/$BASE" "$dir/dist/$BASE.sha256" <<'PY'
import hashlib
import pathlib
import sys

archive = pathlib.Path(sys.argv[1])
sha_file = pathlib.Path(sys.argv[2])
sha_file.write_text(f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n", encoding="utf-8")
PY
  if "$ROOT/scripts/verify-artifacts.sh" "$dir/dist/$BASE" >"$TMP/unsafe-archive.out" 2>"$TMP/unsafe-archive.err"; then
    echo "expected artifact verifier failure for tamper case: unsafe_archive" >&2
    cat "$TMP/unsafe-archive.out" >&2
    exit 1
  fi
}

expect_unsafe_member_type_failure() {
  local dir="$TMP/unsafe_member_type"
  local payload="$TMP/unsafe-member-payload"
  mkdir -p "$dir/dist" "$payload/$PREFIX"
  printf 'ok\n' >"$payload/$PREFIX/README.txt"
  py - "$dir/dist/$BASE" "$payload" "$PREFIX" <<'PY'
import pathlib
import sys
import tarfile

archive = pathlib.Path(sys.argv[1])
payload = pathlib.Path(sys.argv[2])
prefix = sys.argv[3]
with tarfile.open(archive, "w:gz") as tar:
    tar.add(payload / prefix / "README.txt", arcname=f"{prefix}/README.txt")
    link = tarfile.TarInfo(f"{prefix}/link-out")
    link.type = tarfile.SYMTYPE
    link.linkname = "/tmp/code-brain-escape"
    tar.addfile(link)
PY
  cp "$ARCHIVE.sha256" "$dir/dist/$BASE.sha256"
  cp "$(dirname "$ARCHIVE")/$PREFIX.manifest.json" "$dir/dist/$PREFIX.manifest.json"
  cp "$(dirname "$ARCHIVE")/$PREFIX.sbom.json" "$dir/dist/$PREFIX.sbom.json"
  cp "$(dirname "$ARCHIVE")/$PREFIX.provenance.json" "$dir/dist/$PREFIX.provenance.json"
  cp "$(dirname "$ARCHIVE")/$PREFIX.release-notes.md" "$dir/dist/$PREFIX.release-notes.md"
  py - "$dir/dist/$BASE" "$dir/dist/$BASE.sha256" <<'PY'
import hashlib
import pathlib
import sys

archive = pathlib.Path(sys.argv[1])
sha_file = pathlib.Path(sys.argv[2])
sha_file.write_text(f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n", encoding="utf-8")
PY
  if "$ROOT/scripts/verify-artifacts.sh" "$dir/dist/$BASE" >"$TMP/unsafe-member.out" 2>"$TMP/unsafe-member.err"; then
    echo "expected artifact verifier failure for tamper case: unsafe_member_type" >&2
    cat "$TMP/unsafe-member.out" >&2
    exit 1
  fi
}

expect_root_mismatch_failure() {
  local dir="$TMP/root_mismatch"
  local payload="$TMP/root-mismatch-payload"
  local bad_root="not-$PREFIX"
  mkdir -p "$dir/dist" "$payload/$bad_root"
  printf 'ok\n' >"$payload/$bad_root/README.txt"
  py - "$dir/dist/$BASE" "$payload" "$bad_root" <<'PY'
import pathlib
import sys
import tarfile

archive = pathlib.Path(sys.argv[1])
payload = pathlib.Path(sys.argv[2])
bad_root = sys.argv[3]
with tarfile.open(archive, "w:gz") as tar:
    tar.add(payload / bad_root / "README.txt", arcname=f"{bad_root}/README.txt")
PY
  cp "$ARCHIVE.sha256" "$dir/dist/$BASE.sha256"
  cp "$(dirname "$ARCHIVE")/$PREFIX.manifest.json" "$dir/dist/$PREFIX.manifest.json"
  cp "$(dirname "$ARCHIVE")/$PREFIX.sbom.json" "$dir/dist/$PREFIX.sbom.json"
  cp "$(dirname "$ARCHIVE")/$PREFIX.provenance.json" "$dir/dist/$PREFIX.provenance.json"
  cp "$(dirname "$ARCHIVE")/$PREFIX.release-notes.md" "$dir/dist/$PREFIX.release-notes.md"
  py - "$dir/dist/$BASE" "$dir/dist/$BASE.sha256" <<'PY'
import hashlib
import pathlib
import sys

archive = pathlib.Path(sys.argv[1])
sha_file = pathlib.Path(sys.argv[2])
sha_file.write_text(f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n", encoding="utf-8")
PY
  if "$ROOT/scripts/verify-artifacts.sh" "$dir/dist/$BASE" >"$TMP/root-mismatch.out" 2>"$TMP/root-mismatch.err"; then
    echo "expected artifact verifier failure for tamper case: root_mismatch" >&2
    cat "$TMP/root-mismatch.out" >&2
    exit 1
  fi
}

expect_macos_metadata_failure() {
  local dir="$TMP/macos_metadata"
  local payload="$TMP/macos-metadata-payload"
  mkdir -p "$dir/dist" "$payload/$PREFIX"
  printf 'ok\n' >"$payload/$PREFIX/README.txt"
  printf 'metadata\n' >"$payload/$PREFIX/.DS_Store"
  py - "$dir/dist/$BASE" "$payload" "$PREFIX" <<'PY'
import pathlib
import sys
import tarfile

archive = pathlib.Path(sys.argv[1])
payload = pathlib.Path(sys.argv[2])
prefix = sys.argv[3]
with tarfile.open(archive, "w:gz") as tar:
    tar.add(payload / prefix / "README.txt", arcname=f"{prefix}/README.txt")
    tar.add(payload / prefix / ".DS_Store", arcname=f"{prefix}/.DS_Store")
PY
  cp "$ARCHIVE.sha256" "$dir/dist/$BASE.sha256"
  cp "$(dirname "$ARCHIVE")/$PREFIX.manifest.json" "$dir/dist/$PREFIX.manifest.json"
  cp "$(dirname "$ARCHIVE")/$PREFIX.sbom.json" "$dir/dist/$PREFIX.sbom.json"
  cp "$(dirname "$ARCHIVE")/$PREFIX.provenance.json" "$dir/dist/$PREFIX.provenance.json"
  cp "$(dirname "$ARCHIVE")/$PREFIX.release-notes.md" "$dir/dist/$PREFIX.release-notes.md"
  py - "$dir/dist/$BASE" "$dir/dist/$BASE.sha256" <<'PY'
import hashlib
import pathlib
import sys

archive = pathlib.Path(sys.argv[1])
sha_file = pathlib.Path(sys.argv[2])
sha_file.write_text(f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n", encoding="utf-8")
PY
  if "$ROOT/scripts/verify-artifacts.sh" "$dir/dist/$BASE" >"$TMP/macos-metadata.out" 2>"$TMP/macos-metadata.err"; then
    echo "expected artifact verifier failure for tamper case: macos_metadata" >&2
    cat "$TMP/macos-metadata.out" >&2
    exit 1
  fi
}

expect_install_check_failure checksum tamper_checksum
expect_install_check_failure missing_checksum tamper_missing_checksum
expect_install_check_failure manifest tamper_manifest
expect_install_check_failure missing_manifest tamper_missing_manifest
expect_install_check_failure sbom tamper_sbom
expect_install_check_failure missing_sbom tamper_missing_sbom
expect_install_check_failure provenance tamper_provenance
expect_install_check_failure missing_provenance tamper_missing_provenance
expect_install_check_failure dirty_provenance tamper_dirty_provenance
expect_install_check_failure metadata_version tamper_metadata_version
expect_install_check_failure release_notes tamper_release_notes
expect_install_check_failure release_notes_git_head tamper_release_notes_git_head
expect_install_check_failure release_notes_git_status tamper_release_notes_git_status
expect_install_check_failure missing_release_notes tamper_missing_release_notes
expect_unsafe_archive_failure
expect_unsafe_member_type_failure
expect_root_mismatch_failure
expect_macos_metadata_failure

echo "artifact tamper check ok"
