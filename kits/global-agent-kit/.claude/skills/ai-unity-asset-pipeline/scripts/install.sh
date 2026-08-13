#!/bin/sh
set -eu

SKILL_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
SKILL_NAME=ai-unity-asset-pipeline
CLAUDE_DEST=${HOME}/.claude/skills/${SKILL_NAME}
AGENT_DEST=${HOME}/.agents/skills/${SKILL_NAME}
BIN_DEST=${HOME}/.local/bin

install_skill() {
  destination=$1
  parent=$(dirname "$destination")
  mkdir -p "$parent"
  temporary="${destination}.installing.$$"
  rm -rf "$temporary"
  mkdir -p "$temporary"
  cp -R "$SKILL_DIR"/. "$temporary"/
  rm -rf "$destination"
  mv "$temporary" "$destination"
}

install_skill "$CLAUDE_DEST"
install_skill "$AGENT_DEST"
mkdir -p "$BIN_DEST"
install -m 0755 "$AGENT_DEST/bin/ai-unity-asset" "$BIN_DEST/ai-unity-asset"

printf '%s\n' "installed: $CLAUDE_DEST"
printf '%s\n' "installed: $AGENT_DEST"
printf '%s\n' "installed: $BIN_DEST/ai-unity-asset"
