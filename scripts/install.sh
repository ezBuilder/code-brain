#!/usr/bin/env bash
# Code Brain — one-command, zero-config installer (macOS / Linux).
#
#   bash scripts/install.sh [--global|--no-global] [TARGET_DIR]
#
# Installs Code Brain into a target project: ensures `uv` is present (auto-installs it),
# copies the repo-local runtime into <target>/.ai, wires whichever agent CLIs you use
# (Claude Code / Codex / Antigravity are all configured unconditionally and are inert if
# a CLI is absent), bootstraps the runtime venv, and can install the Claude/Codex
# global kit by merging a managed block into existing user files.
set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET_ARG=""
GLOBAL_MODE="${CODE_BRAIN_INSTALL_GLOBAL:-auto}"
YES=0

usage() {
  cat >&2 <<'EOF'
usage:
  scripts/install.sh [options] [target-repo]

Options:
  --global      install the Claude/Codex global kit, merging with existing files
  --no-global   skip global kit install
  --yes         accept interactive defaults
  -h, --help    show this help

Defaults:
  target: current directory
  global: prompt with default yes in an interactive shell; skip in CI/non-interactive shells
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --global)
      GLOBAL_MODE="1"
      shift
      ;;
    --no-global)
      GLOBAL_MODE="0"
      shift
      ;;
    --yes)
      YES=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      break
      ;;
    -*)
      usage
      exit 2
      ;;
    *)
      if [[ -n "$TARGET_ARG" ]]; then
        usage
        exit 2
      fi
      TARGET_ARG="$1"
      shift
      ;;
  esac
done

if [[ $# -gt 0 ]]; then
  if [[ -n "$TARGET_ARG" || $# -gt 1 ]]; then
    usage
    exit 2
  fi
  TARGET_ARG="$1"
fi

TARGET_ARG="${TARGET_ARG:-$(pwd)}"
if [[ ! -d "$TARGET_ARG" ]]; then
  echo "[code-brain] target dir not found: $TARGET_ARG" >&2
  exit 1
fi
TARGET="$(cd "$TARGET_ARG" && pwd -P)"

is_ci() {
  [[ -n "${CI:-}" || -n "${GITHUB_ACTIONS:-}" || -n "${GITLAB_CI:-}" || -n "${AI_CI:-}" ]]
}

should_install_global() {
  case "$GLOBAL_MODE" in
    1|true|TRUE|yes|YES|on|ON)
      return 0
      ;;
    0|false|FALSE|no|NO|off|OFF)
      return 1
      ;;
    auto|"")
      ;;
    *)
      echo "[code-brain] invalid CODE_BRAIN_INSTALL_GLOBAL: $GLOBAL_MODE" >&2
      exit 2
      ;;
  esac

  if is_ci || [[ ! -t 0 ]]; then
    return 1
  fi
  if [[ "$YES" -eq 1 ]]; then
    return 0
  fi

  local answer
  printf '[code-brain] install global Claude/Codex kit too? [Y/n] ' >&2
  read -r answer
  case "$answer" in
    n|N|no|NO) return 1 ;;
    *) return 0 ;;
  esac
}

INSTALL_GLOBAL=0
if should_install_global; then
  INSTALL_GLOBAL=1
fi

RUNTIME_DEFERRED=0
case "${AI_INSTALL_DEFER_RUNTIME:-0}" in
  1|true|TRUE|yes|YES|on|ON) RUNTIME_DEFERRED=1 ;;
esac

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
if ! command -v git >/dev/null 2>&1; then
  echo "[code-brain] git is required. Install git, then re-run." >&2
  exit 1
fi
if ! git -C "$TARGET" rev-parse --show-toplevel >/dev/null 2>&1; then
  git -C "$TARGET" init >/dev/null
fi

# 2. Copy + wire repo-local config.
echo "[code-brain] installing into: $TARGET" >&2
if [[ "$RUNTIME_DEFERRED" -eq 0 ]]; then
  AI_INSTALL_STRICT=1 bash "$SOURCE_ROOT/scripts/install-into.sh" install "$TARGET"
else
  bash "$SOURCE_ROOT/scripts/install-into.sh" install "$TARGET"
fi

# 3. install-into.sh owns bootstrap, index repair, first-session creation, and
# the requested strict health result. Do not repeat any runtime command here.

if [[ "$INSTALL_GLOBAL" -eq 1 ]]; then
  if [[ ! -x "$SOURCE_ROOT/kits/global-agent-kit/install.sh" ]]; then
    echo "[code-brain] global kit installer missing or not executable" >&2
    exit 2
  fi
  ( cd "$SOURCE_ROOT/kits/global-agent-kit" && ./install.sh --all --yes >/dev/null )
  echo "[code-brain] global Claude/Codex kit installed by managed merge." >&2
else
  echo "[code-brain] global Claude/Codex kit skipped. Use --global to install it." >&2
fi

if [[ "$RUNTIME_DEFERRED" -eq 1 ]]; then
  echo "[code-brain] files staged. Runtime activation was deferred for $TARGET." >&2
else
  echo "[code-brain] installed. New AI sessions in $TARGET now load Code Brain memory, search, hooks, and MCP automatically." >&2
fi
echo "[code-brain] (optional) cross-machine sync: set memory_sync.enabled: true in .ai/config.yaml + AI_REMOTE_FETCH=1." >&2
