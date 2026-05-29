#!/usr/bin/env bash
# Install / refresh the user-global Antigravity wiring for Code Brain.
#
# Does two things idempotently:
#   1. Copies scripts/code-brain-mcp-wrapper.sh to ~/.local/bin/code-brain-mcp
#      (executable, dynamic cwd resolution so any workspace works).
#   2. Registers that wrapper as the "code-brain" MCP server in
#      ~/.gemini/antigravity/mcp_config.json, preserving every other server.
#
# OPT-IN (writes to USER-GLOBAL state): this is the ONLY part of Code Brain that
# touches global config (~/.local/bin, ~/.gemini/antigravity/), so it is never run by
# the default install and does nothing unless you explicitly opt in with
# AI_INSTALL_GLOBAL_ANTIGRAVITY=1. It exists only because Antigravity 1.0.x reads its
# MCP server list from the global path, not the per-workspace .agents/. Re-running is
# safe — both steps are idempotent merges.
set -euo pipefail
umask 077

if [[ "${AI_INSTALL_GLOBAL_ANTIGRAVITY:-0}" != "1" ]]; then
    echo "setup-antigravity-global: skipped — writes to GLOBAL config. Set AI_INSTALL_GLOBAL_ANTIGRAVITY=1 to opt in." >&2
    exit 0
fi

SOURCE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WRAPPER_SRC="$SOURCE_ROOT/scripts/code-brain-mcp-wrapper.sh"
WRAPPER_DST="$HOME/.local/bin/code-brain-mcp"

if [[ ! -f "$WRAPPER_SRC" ]]; then
    echo "setup-antigravity-global failed: wrapper template missing at $WRAPPER_SRC" >&2
    exit 2
fi

mkdir -p "$(dirname "$WRAPPER_DST")"
install -m 0755 "$WRAPPER_SRC" "$WRAPPER_DST"
echo "wrapper installed: $WRAPPER_DST"

python3 - "$WRAPPER_DST" "$SOURCE_ROOT" <<'PY'
import sys
from pathlib import Path

wrapper = Path(sys.argv[1])
source_root = Path(sys.argv[2])
sys.path.insert(0, str(source_root / ".ai" / "runtime" / "src"))
from ai_core.mcp_config import install_global_antigravity_mcp

target = install_global_antigravity_mcp(wrapper)
print(f"global mcp_config updated: {target}")
PY
