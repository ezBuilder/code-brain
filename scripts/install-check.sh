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

"$ROOT/scripts/verify-artifacts.sh" "$ARCHIVE" >/dev/null

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

tar -C "$TMP" -xzf "$ARCHIVE"
PACKAGE_DIRS="$TMP/package-dirs.txt"
find "$TMP" -maxdepth 1 -type d -name 'code-brain-*' | sort >"$PACKAGE_DIRS"
PACKAGE_DIR_COUNT="$(wc -l <"$PACKAGE_DIRS" | tr -d ' ')"

if [[ "$PACKAGE_DIR_COUNT" -ne 1 ]]; then
  printf 'package archive must contain exactly one code-brain-* root, got %s\n' "$PACKAGE_DIR_COUNT" >&2
  exit 2
fi

PKG_DIR="$(head -n 1 "$PACKAGE_DIRS")"
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
