#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

MODE="once"
SLEEP_SECONDS=3600

while (($#)); do
  case "$1" in
    --once) MODE="once" ;;
    --forever) MODE="forever" ;;
    --sleep)
      shift
      [[ $# -gt 0 ]] || { echo "--sleep requires seconds" >&2; exit 2; }
      SLEEP_SECONDS="$1"
      ;;
    -h|--help)
      cat <<'USAGE'
Usage: ./scripts/dev-loop.sh [--once|--forever] [--sleep seconds]

Runs the research/evaluate/verify development loop. It snapshots official source freshness,
checks the adopted candidate list, and verifies the implementation. Code changes are still
made by the active coding agent, not by this shell script.
USAGE
      exit 0
      ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
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
  printf 'dev-loop iteration %s start\n' "$iteration"
  snapshot="$(./scripts/research-snapshot.sh)"
  printf 'research snapshot: %s\n' "$snapshot"
  ./scripts/evolve-capture.sh \
    --source dev-loop \
    --candidate "research freshness snapshot" \
    --signal "verified" \
    --note "$snapshot" \
    --tokens 600 \
    --confidence 0.7 \
    --risk 0.1 \
    --tag research \
    --tag evolution >/dev/null
  ./scripts/evolve-score.py --limit 5 >"${XDG_STATE_HOME:-$HOME/.local/state}/code-brain-global-kit/evolution/latest-score.json"
  python3 - "${XDG_STATE_HOME:-$HOME/.local/state}/code-brain-global-kit/evolution/latest-score.json" "${XDG_STATE_HOME:-$HOME/.local/state}/code-brain-global-kit/evolution/top-context.txt" <<'PY'
import json
import sys
from pathlib import Path

score = json.loads(Path(sys.argv[1]).read_text())
lines = []
for item in score.get("candidates", [])[:3]:
    lines.append(
        f"{item['candidate']}: decision={item['decision']} confidence={item['confidence']} risk={item['risk']}"
    )
Path(sys.argv[2]).write_text("\n".join(lines) + ("\n" if lines else ""))
PY
  ./scripts/evolve-promote.sh --dry-run >/dev/null

  if ! rg -q 'Claude user slash commands.*채택' docs/AI_DEV_LOOP.md; then
    echo "adopted slash command candidate missing from docs/AI_DEV_LOOP.md" >&2
    exit 1
  fi
  if ! rg -q 'Claude SessionStart hook.*채택' docs/AI_DEV_LOOP.md; then
    echo "adopted SessionStart candidate missing from docs/AI_DEV_LOOP.md" >&2
    exit 1
  fi

  ./scripts/validate.sh
  ./scripts/doctor.sh
  ./scripts/evolve-snapshot.sh snapshot >/dev/null
  printf 'dev-loop iteration %s ok\n' "$iteration"
}

while :; do
  run_iteration
  if [[ "$MODE" == "once" ]]; then
    break
  fi
  sleep "$SLEEP_SECONDS"
done
