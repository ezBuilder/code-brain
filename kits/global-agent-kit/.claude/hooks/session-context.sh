#!/usr/bin/env bash
set -euo pipefail

state_dir="${XDG_STATE_HOME:-$HOME/.local/state}/code-brain-global-kit/evolution"
summary_file="$state_dir/top-context.txt"

base_context="code-brain-global-kit is installed globally. Use /kit-doctor for install diagnostics, /kit-research before kit changes, and /kit-upgrade-loop for one autonomous research/evaluate/implement/verify iteration. Keep production secrets and destructive actions approval-gated."

evolution_context=""
if [[ -f "$summary_file" ]]; then
  evolution_context="$(python3 - "$summary_file" <<'PY'
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(errors="replace")
lines = [line.strip() for line in text.splitlines() if line.strip()]
print(" ".join(lines[:3])[:600])
PY
)"
fi

python3 - "$base_context" "$evolution_context" <<'PY'
import json
import sys

base = sys.argv[1]
evolution = sys.argv[2].strip()
context = base
if evolution:
    context += " Code Brain Evolution top context: " + evolution

print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": context
    }
}, ensure_ascii=False))
PY
