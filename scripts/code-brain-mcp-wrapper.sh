#!/usr/bin/env bash
# Code Brain MCP launcher used by Antigravity's user-global mcp_config.json.
# Antigravity 1.0.x does not yet read workspace .agents/mcp_config.json, so the
# same global entry must work in every project that installs Code Brain. This
# wrapper resolves the right .ai/bin/ai-mcp at run time by walking up from the
# spawning cwd until it finds one; otherwise it honors AI_HOME / CODE_BRAIN_ROOT
# or exits cleanly so Antigravity does not poison its tool list.
set -euo pipefail
umask 077

find_ai_mcp() {
    local dir="$1"
    while [[ "$dir" != "/" && -n "$dir" ]]; do
        if [[ -x "$dir/.ai/bin/ai-mcp" ]]; then
            printf '%s\n' "$dir/.ai/bin/ai-mcp"
            return 0
        fi
        dir="$(dirname "$dir")"
    done
    return 1
}

cwd_now="${PWD:-$(pwd)}"
target=""
if target="$(find_ai_mcp "$cwd_now")"; then
    :
elif [[ -n "${AI_HOME:-}" && -x "$AI_HOME/.ai/bin/ai-mcp" ]]; then
    target="$AI_HOME/.ai/bin/ai-mcp"
elif [[ -n "${CODE_BRAIN_ROOT:-}" && -x "$CODE_BRAIN_ROOT/.ai/bin/ai-mcp" ]]; then
    target="$CODE_BRAIN_ROOT/.ai/bin/ai-mcp"
else
    echo "code-brain-mcp: no .ai/bin/ai-mcp found above $cwd_now (set AI_HOME or install Code Brain)" >&2
    exit 0
fi

exec "$target" "$@"
