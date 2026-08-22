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
| `code_retrieval` | production `search.query` golden-query Recall@K, MRR, NDCG@K, latency (incl. camelCase subtoken recall) | temporary production index |
| `line_span_retrieval` | production `context_pack v2` file-vs-span Recall@K and rank-weighted coverage | temporary production index |
| `memory_retrieval` | production `recall_memory` golden-query Recall@K/MRR incl. expiry/refuted/tombstone liveness filtering | temporary memory store |
| `skill_drift` | installed skills' body-sha256 matches catalog | `ai skills list` |
| `precall_overrides` | user override ratio stays below auto-disable threshold | audit |

## Running

```bash
make eval
uv run --project .ai/runtime python .ai/evals/run.py --all --wired --json
# Run the span contract directly:
uv run --project .ai/runtime python .ai/evals/run.py --axis line_span_retrieval --wired --strict --json
```

Eval runs are read-only and never write to `.ai/memory/`.

`make eval` is the strict complete gate for the currently supported axes:
`precall_routing`, `context_budget`, `tool_discovery`, `autoresearch_retrieval`,
`code_retrieval`, and `memory_retrieval`.
Retrieval cases write only throwaway indexes under the system temporary directory;
they never touch repo memory or the real index. `--all --wired` also reports planned
axes, but unsupported axes remain explicitly `skipped`; they are never counted
as passing. Add `--require-complete` when skipped cases must fail the command.

`line_span_retrieval` builds a temporary production index, calls `context_pack` in
`refs-only` mode, and scores its verified lexical path/span references. It never
touches the repository's real index or memory. The pure evaluator remains directly
unit-tested so metric changes cannot masquerade as retrieval improvements.

## Line/span contract

Each `qrels` item is `{path, start_line, end_line}`. Paths are canonical relative
POSIX paths; spans are one-based, inclusive, positive, non-reversed, and bounded
to 1,000,000. Ranked candidates may omit both line fields: a matching path then
counts only as a **file hit**, never as a span hit. A candidate span overlaps an
expected span when `min(candidate_end, expected_end) - max(candidate_start,
expected_start) + 1 > 0`.

The evaluator reports:

- `file_recall_at_k`: unique expected files found in the first K candidates,
  divided by unique expected files.
- `exact_span_recall_at_k`: expected spans with an exact path/start/end candidate
  in the first K, divided by qrel spans.
- `overlap_span_recall_at_k`: expected spans with any positive inclusive overlap
  on the same path in the first K, divided by qrel spans.
- `rank_weighted_span_coverage`: macro-average of the best
  `overlap_fraction / rank`, where `overlap_fraction` is intersecting expected
  lines divided by expected-span length and rank is one-based.

Every metric returns `0.0` when its denominator is zero. Invalid, negative,
reversed, overlarge spans and absolute/traversal/non-canonical paths are rejected
before scoring. Output ordering and numeric rounding are canonical and stable.

## Status

The offline runner currently wires `precall_routing`, `context_budget`,
`tool_discovery`, `autoresearch_retrieval`, `code_retrieval`,
`line_span_retrieval`, and `memory_retrieval` to their production implementations.
`memory_retrieval` fixtures pin recency via `now` but place expiry bounds in
the far past/future — decision liveness folds against the wall clock, and a
near-now bound would make the axis flaky.
`decision_logging` remains unsupported until
there is a real prompt-to-memory production path to exercise; its cases stay
visible as skipped work rather than producing synthetic success.
