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
./scripts/preflight.sh --check-only --json --proof-file .ai/cache/preflight-proof.json
./scripts/env-check.sh
SYNC_ARGS=(sync --no-progress --project .ai/runtime)
case "${AI_INSTALL_DENSE:-0}" in
  1|true|TRUE|yes|YES|on|ON) SYNC_ARGS+=(--extra dense) ;;
esac
if ! uv "${SYNC_ARGS[@]}"; then
  EXISTING_PYTHON=""
  if [[ -x ".ai/runtime/.venv/bin/python" ]]; then
    EXISTING_PYTHON=".ai/runtime/.venv/bin/python"
  elif [[ -x ".ai/runtime/.venv/Scripts/python.exe" ]]; then
    EXISTING_PYTHON=".ai/runtime/.venv/Scripts/python.exe"
  fi
  if [[ "$LOW_MEMORY" -eq 1 && -n "$EXISTING_PYTHON" ]] && "$EXISTING_PYTHON" -c 'import ai_core.cli'; then
    echo "bootstrap warning: uv sync failed; retaining the verified existing runtime" >&2
  else
    echo "bootstrap failed: uv sync did not complete and no usable existing runtime is available" >&2
    exit 1
  fi
fi
if git rev-parse --git-dir >/dev/null 2>&1; then
  git config core.hooksPath .githooks
fi
if [[ "$SKIP_RENDER" -eq 0 ]]; then
  uv run --project .ai/runtime ai render --manifest-only --json
fi
if [[ "$SKIP_DOCTOR" -eq 0 ]]; then
  uv run --project .ai/runtime ai doctor --json
fi
