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

## Suite report

A run produces `.ai/generated/evals/<timestamp>.json`:

```json
{
  "axis": "decision_logging",
  "cases": 12,
  "passed": 11,
  "failed": ["case-id-7"],
  "duration_ms": 432
}
```

Failing cases must be reproducible: the report includes the exact input,
the expected assertion, and the observed output.

## Human review

LLM-as-judge axes (e.g. "did the agent's reply restate the decision
correctly") use a separate `human_review: true` flag. Until that lane is
wired, those cases are marked `skipped`, not `passed`.
