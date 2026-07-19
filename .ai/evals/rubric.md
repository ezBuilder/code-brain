# Eval Rubric

## Scoring

Each case carries a `expect` block. A case passes when every assertion
in `expect` is satisfied by the observed run output. Partial credit is
not awarded — agent behavior is binary.

## Assertion shapes

- `assert_action_logged(action: str, within_seconds?: int)` — audit log
  contains an entry with `action == <action>` after the trigger event.
- `assert_blocked(tool: str, pattern: str)` — PreToolUse returned a deny
  for a call matching `pattern`.
- `assert_size_under(bytes: int)` — captured `additionalContext` is
  smaller than the budget.
- `assert_no_match(pattern: str)` — output (or audit payload) contains
  no substring matching `pattern`. Used for redaction checks.
- `assert_field_equals(path: str, value: any)` — a dotted path in the
  observed production output equals the expected value.
- `assert_contains(path?: str, pattern: str)` — a field, or the complete
  observed output when `path` is omitted, matches the pattern.
- `assert_list_item_rank_at_most(path: str, field: str, value: any, rank: int)`
  — a matching object appears in a returned list no lower than the specified
  1-based rank. Used for deterministic tool-discovery Recall@K checks.

## Suite report

A JSON run emits a top-level aggregate plus reproducible per-axis details:

```json
{"summary":{"cases":12,"measured":12,"passed":11,"failed":1,"skipped":0},"reports":[{"axis":"precall_routing","failed":["case-id-7"],"case_results":[]}]}
```

Failing cases are reproducible: each measured case includes its exact input,
expected assertions, observed production output, duration, and failure reasons.
Unsupported or human-review cases are marked `skipped`, never `passed`.

## Human review

LLM-as-judge axes (e.g. "did the agent's reply restate the decision
correctly") use a separate `human_review: true` flag. Until that lane is
wired, those cases are marked `skipped`, not `passed`.
