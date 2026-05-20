#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/code-brain-global-kit"
OUT_DIR="$STATE_DIR/research"
OUT_FILE="$OUT_DIR/$(date +%Y%m%d-%H%M%S).md"

mkdir -p "$OUT_DIR"

sources=(
  "Claude Code settings|https://docs.anthropic.com/en/docs/claude-code/settings"
  "Claude Code hooks|https://docs.anthropic.com/en/docs/claude-code/hooks"
  "Claude Code slash commands|https://docs.anthropic.com/en/docs/claude-code/slash-commands"
  "Claude Code subagents|https://docs.anthropic.com/en/docs/claude-code/sub-agents"
  "Claude Code MCP|https://docs.anthropic.com/en/docs/claude-code/mcp"
  "Claude Code memory|https://docs.anthropic.com/en/docs/claude-code/memory"
  "OpenAI Codex CLI|https://github.com/openai/codex"
  "OpenAI Codex CLI help|https://help.openai.com/en/articles/11096431-openai-codex-cli-getting-started"
)

{
  printf '# Research Snapshot\n\n'
  printf '%s\n' "- generated_at: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '%s\n\n' "- repo: $ROOT_DIR"

  for item in "${sources[@]}"; do
    name="${item%%|*}"
    url="${item#*|}"
    printf '## %s\n\n' "$name"
    printf '%s\n' "- url: $url"
    headers="$(curl -fsIL --max-time 12 "$url" 2>/dev/null || true)"
    status="$(printf '%s\n' "$headers" | awk 'index(toupper($0), "HTTP/") == 1 {line=$0} END {print line}')"
    etag="$(printf '%s\n' "$headers" | awk 'BEGIN{IGNORECASE=1} /^etag:/ {sub(/^[^:]+:[[:space:]]*/, ""); value=$0} END {print value}')"
    modified="$(printf '%s\n' "$headers" | awk 'BEGIN{IGNORECASE=1} /^last-modified:/ {sub(/^[^:]+:[[:space:]]*/, ""); value=$0} END {print value}')"
    printf '%s\n' "- status: ${status:-unavailable}"
    printf '%s\n' "- etag: ${etag:-none}"
    printf '%s\n\n' "- last_modified: ${modified:-none}"
  done
} >"$OUT_FILE"

ln -sfn "$OUT_FILE" "$OUT_DIR/latest.md"
printf '%s\n' "$OUT_FILE"
