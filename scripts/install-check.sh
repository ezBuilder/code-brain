#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ARCHIVE="${1:-}"

if [[ -z "$ARCHIVE" ]]; then
  ARCHIVE="$(ls -t "$ROOT"/dist/code-brain-*.tar.gz | head -n 1)"
fi

if [[ ! -f "$ARCHIVE" ]]; then
  echo "archive not found: $ARCHIVE" >&2
  exit 2
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

tar -C "$TMP" -xzf "$ARCHIVE"
PKG_DIR="$(find "$TMP" -maxdepth 1 -type d -name 'code-brain-*' | head -n 1)"

if [[ -z "$PKG_DIR" ]]; then
  echo "package directory not found in archive" >&2
  exit 2
fi

cd "$PKG_DIR"

uv run --project .ai/runtime ai --json version >/dev/null
uv run --project .ai/runtime ai doctor --strict --json >/dev/null
.ai/bin/ai --json version >/dev/null
.ai/bin/ai-hook SessionStart --json <<< '{"agent":"install-check"}' >/dev/null
uv run --project .ai/runtime python -m pytest .ai/runtime/tests >/dev/null

echo "install check ok: $ARCHIVE"
