#!/usr/bin/env bash
# managed-by: code-brain
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

usage() {
  cat >&2 <<'EOF'
usage: scripts/run-observed-command.sh <label> -- <command> [args...]

Set AI_RUNNER_TIMEOUT_SECONDS to a finite positive number to enforce an
explicit deadline. When unset or empty, the observed command has no deadline.
EOF
}

if [[ $# -lt 3 || -z "${1:-}" || "${2:-}" != "--" ]]; then
  usage
  exit 2
fi

label="$1"
shift 2

observer=(
  uv run --project .ai/runtime python scripts/run-observed.py
  --label "$label"
)
if [[ -n "${AI_RUNNER_TIMEOUT_SECONDS:-}" ]]; then
  observer+=(--timeout-seconds "$AI_RUNNER_TIMEOUT_SECONDS")
fi

exec "${observer[@]}" -- "$@"