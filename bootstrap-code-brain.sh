#!/usr/bin/env bash
set -euo pipefail
umask 077
cd "$(dirname "$0")"

SKIP_DOCTOR=0
SKIP_RENDER=0
LOW_MEMORY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-doctor) SKIP_DOCTOR=1 ;;
    --skip-render) SKIP_RENDER=1 ;;
    --low-memory) LOW_MEMORY=1 ;;
    *)
      echo "usage: ./bootstrap-code-brain.sh [--skip-doctor] [--skip-render] [--low-memory]" >&2
      exit 2
      ;;
  esac
  shift
done

case "${AI_BOOTSTRAP_LOW_MEMORY:-0}" in
  1|true|TRUE|yes|YES|on|ON) LOW_MEMORY=1 ;;
esac
if [[ "$LOW_MEMORY" -eq 1 ]]; then
  export UV_CONCURRENT_DOWNLOADS="${UV_CONCURRENT_DOWNLOADS:-1}"
  export UV_CONCURRENT_BUILDS="${UV_CONCURRENT_BUILDS:-1}"
  export UV_CONCURRENT_INSTALLS="${UV_CONCURRENT_INSTALLS:-1}"
  export CMAKE_BUILD_PARALLEL_LEVEL="${CMAKE_BUILD_PARALLEL_LEVEL:-1}"
  export MAKEFLAGS="${MAKEFLAGS:--j1}"
fi

mkdir -p .ai/cache
./scripts/preflight.sh --check-only --json --proof-file .ai/cache/preflight-proof.json >/dev/null
./scripts/env-check.sh >/dev/null
case "${AI_INSTALL_DENSE:-0}" in
  1|true|TRUE|yes|YES|on|ON)
    uv sync --no-progress --project .ai/runtime --extra dense
    ;;
  *)
    uv sync --no-progress --project .ai/runtime
    ;;
esac
if git rev-parse --git-dir >/dev/null 2>&1; then
  git config core.hooksPath .githooks
fi
if [[ "$SKIP_RENDER" -eq 0 ]]; then
  uv run --project .ai/runtime ai render --manifest-only --json >/dev/null
fi
if [[ "$SKIP_DOCTOR" -eq 0 ]]; then
  uv run --project .ai/runtime ai doctor --json >/dev/null
fi
