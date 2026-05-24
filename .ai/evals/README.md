# Code Brain Evals

Task-specific evaluations for Code Brain itself. The goal is to catch
regressions in agent-facing behavior that unit tests cannot — e.g.
"did `record_decision` actually fire when the user said 이걸로 가자?",
"did PreToolUse correctly route `rg -n foo .` to the sandbox?".

## Layout

- `cases/` — input/expected JSONL files, one per axis.
- `rubric.md` — scoring rules for human-in-the-loop and LLM judges.
- `run.py` — runner that executes a suite against the current build and
  writes a results file under `.ai/generated/evals/<timestamp>.json`.

## Axes (initial)

| Axis | What it measures | Source signal |
|---|---|---|
| `decision_logging` | record_decision fires on user lock-in phrases | audit + transcript |
| `precall_routing` | PreToolUse blocks long-output Bash; allows safe one-shots | audit |
| `memory_injection_size` | SessionStart additionalContext stays under budget | hook telemetry |
| `skill_drift` | installed skills' body-sha256 matches catalog | `ai skills list` |
| `precall_overrides` | user override ratio stays below auto-disable threshold | audit |

## Running

```
.ai/runtime/.venv/bin/python .ai/evals/run.py --axis decision_logging
.ai/runtime/.venv/bin/python .ai/evals/run.py --all --json
```

Eval runs are read-only and never write to `.ai/memory/`.

## Status

Skeleton only — cases and runner are stubs. First real axis to wire is
`memory_injection_size` since the telemetry already exists in audit.
