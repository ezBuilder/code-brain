# Code Brain Evals

Task-specific evaluations for Code Brain itself. The goal is to catch
regressions in agent-facing behavior that unit tests cannot — e.g.
"did `record_decision` actually fire when the user said 이걸로 가자?",
"did PreToolUse correctly route `rg -n foo .` to the sandbox?".

## Layout

- `cases/` — input/expected JSONL files, one per axis.
- `rubric.md` — scoring rules for human-in-the-loop and LLM judges.
- `run.py` — deterministic, read-only runner that calls production decision
  functions directly and emits a reproducible report.

## Axes (initial)

| Axis | What it measures | Source signal |
|---|---|---|
| `decision_logging` | record_decision fires on user lock-in phrases | audit + transcript |
| `precall_routing` | PreToolUse blocks long-output Bash; allows safe one-shots | audit |
| `context_budget` | result caps, byte truncation, and protected-signal retention | production function |
| `tool_discovery` | natural-language intents retrieve the correct MCP tool within a bounded rank | production function |
| `autoresearch_retrieval` | production FTS ranking preserves Recall@K, MRR, and NDCG@K smoke baselines | temporary production index |
| `skill_drift` | installed skills' body-sha256 matches catalog | `ai skills list` |
| `precall_overrides` | user override ratio stays below auto-disable threshold | audit |

## Running

```bash
make eval
uv run --project .ai/runtime python .ai/evals/run.py --all --wired --json
```

Eval runs are read-only and never write to `.ai/memory/`.

`make eval` is the strict complete gate for the currently supported axes:
`precall_routing`, `context_budget`, `tool_discovery`, and `autoresearch_retrieval`.
Retrieval cases write only throwaway indexes under the system temporary directory;
they never touch repo memory or the real index. `--all --wired` also reports planned
axes, but unsupported axes remain explicitly `skipped`; they are never counted
as passing. Add `--require-complete` when skipped cases must fail the command.

## Status

The offline runner currently wires `precall_routing`, `context_budget`,
`tool_discovery`, and `autoresearch_retrieval` to their production implementations.
`decision_logging` remains unsupported until
there is a real prompt-to-memory production path to exercise; its cases stay
visible as skipped work rather than producing synthetic success.
