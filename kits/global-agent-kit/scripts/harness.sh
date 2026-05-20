#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

MODE="once"
INSTALL=0
SLEEP_SECONDS=900

usage() {
  cat <<'USAGE'
Usage: ./scripts/harness.sh [--once|--forever] [--install] [--sleep seconds]

Runs the commercial-readiness loop for this kit:
  1. validate repository assets
  2. doctor installed Claude/Codex assets
  3. dry-run installer
  4. optionally install globally

Use --forever only from an explicit long-running terminal/tmux session.
USAGE
}

while (($#)); do
  case "$1" in
    --once) MODE="once" ;;
    --forever) MODE="forever" ;;
    --install) INSTALL=1 ;;
    --sleep)
      shift
      [[ $# -gt 0 ]] || { echo "--sleep requires seconds" >&2; exit 2; }
      SLEEP_SECONDS="$1"
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if ! [[ "$SLEEP_SECONDS" =~ ^[0-9]+$ ]] || [[ "$SLEEP_SECONDS" -lt 1 ]]; then
  echo "--sleep must be a positive integer" >&2
  exit 2
fi

iteration=0

run_iteration() {
  iteration=$((iteration + 1))
  printf 'harness iteration %s start\n' "$iteration"

  ./scripts/validate.sh
  ./scripts/doctor.sh
  ./install.sh --all --dry-run >/dev/null

  if [[ "$INSTALL" -eq 1 ]]; then
    ./install.sh --all --yes
  fi

  printf 'harness iteration %s ok\n' "$iteration"
}

while :; do
  run_iteration
  if [[ "$MODE" == "once" ]]; then
    break
  fi
  sleep "$SLEEP_SECONDS"
done
