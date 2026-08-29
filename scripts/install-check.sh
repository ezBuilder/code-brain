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
# `ai-hook SessionStart` below registers DETACHED children (index rebuild, cache
# refresh) that keep writing under $TMP after the hook returns. Deleting the tree
# while they run made `rm -rf` fail with "Directory not empty" and turned a passing
# install check into a red release gate. Wait for the children this check created,
# then remove the tree; retry briefly so a slow exit degrades to a retry, not a failure.
cleanup() {
  wait_for_spawned_children
  local attempt
  for attempt in 1 2 3 4 5; do
    if rm -rf "$TMP" 2>/dev/null; then
      return 0
    fi
    sleep 1
  done
  rm -rf "$TMP" 2>/dev/null || true
}
trap cleanup EXIT

# Poll the Code Brain child-process registry for pids this check spawned and wait
# for them to exit, bounded so a stuck child cannot hang the release gate.
wait_for_spawned_children() {
  local registry deadline pid alive
  registry="$TMP"/*/.ai/cache/child-processes.jsonl
  deadline=$(( $(date +%s) + 30 ))
  while (( $(date +%s) < deadline )); do
    alive=0
    for registry in "$TMP"/*/.ai/cache/child-processes.jsonl; do
      [[ -f "$registry" ]] || continue
      while read -r pid; do
        [[ -n "$pid" ]] || continue
        if kill -0 "$pid" 2>/dev/null; then
          alive=1
        fi
      done < <(sed -n 's/.*"pid"[[:space:]]*:[[:space:]]*\([0-9]\{1,\}\).*/\1/p' "$registry")
    done
    (( alive == 0 )) && return 0
    sleep 1
  done
  return 0
}

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

echo "install check ok: $ARCHIVE"
