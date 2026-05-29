#!/usr/bin/env bash
# Code Brain — one-command, zero-config installer (macOS / Linux).
#
#   bash scripts/install.sh [TARGET_DIR]      # TARGET_DIR defaults to the current dir
#
# Installs Code Brain into a target project: ensures `uv` is present (auto-installs it),
# copies the repo-local runtime into <target>/.ai, wires whichever agent CLIs you use
# (Claude Code / Codex / Antigravity are all configured unconditionally and are inert if
# a CLI is absent), and bootstraps the runtime venv. It writes NOTHING to your global /
# machine-wide config. Windows peer: scripts/install.ps1.
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET_ARG="${1:-$(pwd)}"
if [[ ! -d "$TARGET_ARG" ]]; then
  echo "[code-brain] target dir not found: $TARGET_ARG" >&2
  exit 1
fi
TARGET="$(cd "$TARGET_ARG" && pwd -P)"

# 1. uv is the only hard prerequisite (it provisions Python 3.11+ itself). Auto-install.
if ! command -v uv >/dev/null 2>&1; then
  echo "[code-brain] 'uv' not found — installing from astral.sh ..." >&2
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "[code-brain] uv install failed. Install it manually: https://docs.astral.sh/uv/ , then re-run." >&2
  exit 1
fi

# 2. Copy + wire repo-local config (repo-local only; never global).
echo "[code-brain] installing into: $TARGET" >&2
bash "$SOURCE_ROOT/scripts/install-into.sh" install "$TARGET"

# 3. Bootstrap the runtime (uv sync → venv, manifest, doctor). install-into.sh wrote
#    bootstrap-code-brain.sh into the target.
if [[ -f "$TARGET/bootstrap-code-brain.sh" ]]; then
  ( cd "$TARGET" && bash ./bootstrap-code-brain.sh )
else
  ( cd "$TARGET" && uv sync --project .ai/runtime --extra dense )
fi

echo "[code-brain] done. claude / codex / agy in $TARGET now share Code Brain memory." >&2
echo "[code-brain] (optional) cross-machine sync: set memory_sync.enabled: true in .ai/config.yaml + AI_REMOTE_FETCH=1." >&2
