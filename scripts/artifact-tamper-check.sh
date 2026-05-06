#!/usr/bin/env bash
set -euo pipefail
export COPYFILE_DISABLE=1

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
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
  python - "$dir/dist/$BASE.sha256" <<'PY'
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
text = path.read_text(encoding="utf-8").split(maxsplit=1)
path.write_text("0" * 64 + "  " + text[1], encoding="utf-8")
PY
}

tamper_manifest() {
  local dir="$1"
  python - "$dir/dist/$PREFIX.manifest.json" <<'PY'
import json
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
payload["files"][0]["sha256"] = "0" * 64
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

tamper_sbom() {
  local dir="$1"
  python - "$dir/dist/$PREFIX.sbom.json" <<'PY'
import json
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
payload["lockfile_sha256"] = "0" * 64
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

tamper_provenance() {
  local dir="$1"
  python - "$dir/dist/$PREFIX.provenance.json" <<'PY'
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

tamper_release_notes() {
  local dir="$1"
  python - "$dir/dist/$PREFIX.release-notes.md" <<'PY'
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
path.write_text(text.replace("Release Notes", "Release Log", 1), encoding="utf-8")
PY
}

expect_unsafe_archive_failure() {
  local dir="$TMP/unsafe_archive"
  local payload="$TMP/unsafe-payload"
  mkdir -p "$dir/dist" "$payload/$PREFIX"
  printf 'ok\n' >"$payload/$PREFIX/README.txt"
  printf 'bad\n' >"$payload/../escape.txt"
  python - "$dir/dist/$BASE" "$payload" "$PREFIX" <<'PY'
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
  python - "$dir/dist/$BASE" "$dir/dist/$BASE.sha256" <<'PY'
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

expect_install_check_failure checksum tamper_checksum
expect_install_check_failure manifest tamper_manifest
expect_install_check_failure sbom tamper_sbom
expect_install_check_failure provenance tamper_provenance
expect_install_check_failure release_notes tamper_release_notes
expect_unsafe_archive_failure

echo "artifact tamper check ok"
