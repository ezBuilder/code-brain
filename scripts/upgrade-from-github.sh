#!/usr/bin/env bash
set -euo pipefail
umask 077

REPO_URL="${CODE_BRAIN_REPO_URL:-https://github.com/ezBuilder/code-brain.git}"
REF="${CODE_BRAIN_REF:-main}"
ACTION="auto"
DRY_RUN=0
KEEP_CLONE="${CODE_BRAIN_KEEP_UPGRADE_CLONE:-0}"
TARGET_ARG=""
GLOBAL_INSTALL_ARG=()

usage() {
  cat >&2 <<'EOF'
usage:
  scripts/upgrade-from-github.sh [options] [target-repo]

Options:
  --install             install into target
  --upgrade             upgrade existing target
  --repo-url URL        Code Brain git repo URL
  --repo URL            alias for --repo-url
  --ref REF             branch, tag, or commit to install
  --dry-run             print the planned action only
  --global              install/refresh the Claude/Codex global kit by managed merge
  --no-global           skip global kit install
  --keep-clone          keep the temporary clone for debugging

Defaults:
  repo: https://github.com/ezBuilder/code-brain.git
  ref:  main
  target: current directory
  global: first install prompts in interactive shells; CI/non-interactive shells skip unless --global

Antigravity global setup remains opt-in.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --install)
      ACTION="install"
      shift
      ;;
    --upgrade)
      ACTION="upgrade"
      shift
      ;;
    --repo-url|--repo)
      REPO_URL="${2:-}"
      [[ -n "$REPO_URL" ]] || { usage; exit 2; }
      shift 2
      ;;
    --ref)
      REF="${2:-}"
      [[ -n "$REF" ]] || { usage; exit 2; }
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --global)
      GLOBAL_INSTALL_ARG=(--global)
      shift
      ;;
    --no-global)
      GLOBAL_INSTALL_ARG=(--no-global)
      shift
      ;;
    --keep-clone)
      KEEP_CLONE=1
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
  exit 2
fi
TARGET="$(cd "$TARGET_ARG" && pwd -P)"
if [[ "$ACTION" == "auto" ]]; then
  if [[ -x "$TARGET/.ai/bin/ai" || -f "$TARGET/.ai/generated/install-manifest.json" ]]; then
    ACTION="upgrade"
  else
    ACTION="install"
  fi
fi

if [[ "$DRY_RUN" == "1" ]]; then
  printf '[code-brain] dry-run repo=%s ref=%s action=%s target=%s global=%s\n' "$REPO_URL" "$REF" "$ACTION" "$TARGET" "${GLOBAL_INSTALL_ARG[*]:-auto}"
  exit 0
fi

command -v git >/dev/null 2>&1 || { echo "[code-brain] git is required" >&2; exit 2; }
command -v bash >/dev/null 2>&1 || { echo "[code-brain] bash is required" >&2; exit 2; }

TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/code-brain-upgrade.XXXXXX")"
cleanup() {
  if [[ "$KEEP_CLONE" != "1" ]]; then
    rm -rf "$TMP_ROOT"
  else
    echo "[code-brain] kept clone: $TMP_ROOT" >&2
  fi
}
trap cleanup EXIT

CHECKOUT="$TMP_ROOT/code-brain"
git clone --depth 1 "$REPO_URL" "$CHECKOUT" >/dev/null
if [[ "$REF" != "HEAD" ]]; then
  if git -C "$CHECKOUT" fetch --depth 1 origin "$REF" >/dev/null 2>&1; then
    git -C "$CHECKOUT" checkout --detach FETCH_HEAD >/dev/null
  else
    git -C "$CHECKOUT" checkout --detach "$REF" >/dev/null
  fi
fi

export CODE_BRAIN_REPO_URL="$REPO_URL"
export CODE_BRAIN_REF="$REF"
if [[ "$ACTION" == "install" ]]; then
  bash "$CHECKOUT/scripts/install.sh" "${GLOBAL_INSTALL_ARG[@]}" "$TARGET"
else
  AI_INSTALL_DEFER_RUNTIME=1 bash "$CHECKOUT/scripts/install-into.sh" upgrade "$TARGET"
  if [[ -f "$TARGET/bootstrap-code-brain.sh" ]]; then
    ( cd "$TARGET" && AI_BOOTSTRAP_LOW_MEMORY=1 bash ./bootstrap-code-brain.sh --skip-doctor --skip-render --low-memory )
  fi
  ( cd "$TARGET" && .ai/bin/ai session start --agent operator --rebuild auto --repair-audit-index --render-manifest --json )
  ( cd "$TARGET" && .ai/bin/ai doctor --strict --json )
  if [[ "${GLOBAL_INSTALL_ARG[*]:-}" == "--global" ]]; then
    ( cd "$CHECKOUT/kits/global-agent-kit" && ./install.sh --all --yes >/dev/null )
    echo "[code-brain] global Claude/Codex kit refreshed by managed merge" >&2
  fi
  echo "[code-brain] upgraded from $REPO_URL@$REF into $TARGET" >&2
fi
