#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="$("$ROOT/.ai/bin/ai" --json version | python -c 'import json,sys; print(json.load(sys.stdin)["version"])')"
OUT_DIR="$ROOT/dist"
NAME="code-brain-${VERSION}"
ARCHIVE="$OUT_DIR/${NAME}.tar.gz"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$OUT_DIR"
rm -f "$ARCHIVE"
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

python - "$ARCHIVE" <<'PY'
import hashlib
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
digest = hashlib.sha256(path.read_bytes()).hexdigest()
path.with_suffix(path.suffix + ".sha256").write_text(f"{digest}  {path.name}\n", encoding="utf-8")
print(path)
print(path.with_suffix(path.suffix + ".sha256"))
PY
