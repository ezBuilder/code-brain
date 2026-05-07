#!/usr/bin/env bash
set -euo pipefail
export COPYFILE_DISABLE=1

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ARCHIVE="${1:-}"
if [[ -z "$ARCHIVE" ]]; then
  ARCHIVE="$(ls -t "$ROOT"/dist/code-brain-*.tar.gz 2>/dev/null | head -n 1 || true)"
fi
if [[ -z "$ARCHIVE" || ! -f "$ARCHIVE" ]]; then
  echo "no primary archive" >&2
  exit 2
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
REBUILD_DIR="$TMP/rebuild"
mkdir -p "$REBUILD_DIR"

DIST_OVERRIDE="$REBUILD_DIR" "$ROOT/scripts/package.sh" >/dev/null
REBUILD="$(ls -t "$REBUILD_DIR"/code-brain-*.tar.gz | head -n 1)"
PRIMARY_SHA="$(shasum -a 256 "$ARCHIVE" | awk '{print $1}')"
REBUILD_SHA="$(shasum -a 256 "$REBUILD" | awk '{print $1}')"

if [[ "$PRIMARY_SHA" != "$REBUILD_SHA" ]]; then
  echo "reproducibility drift: primary=$PRIMARY_SHA rebuild=$REBUILD_SHA" >&2
  exit 1
fi

echo "reproducibility check ok"
