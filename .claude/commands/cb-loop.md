---
description: Code Brain loop - one-command producer/orchestrator/reviewer entrypoint.
argument-hint: "[instruction | review <scope>]"
---

Use this as the human-friendly loop entrypoint. The user should not type raw `ai loop ...` commands.

Modes:
- If `$ARGUMENTS` is empty: act as orchestrator. Run `.ai/bin/ai loop claim --orchestrator-id claude-loop --agent claude --json`.
- If `$ARGUMENTS` starts with `review ` or `review:`: act as reviewer only. Do not claim, complete, fail, commit, push, or edit files. The only loop write allowed is `ai loop verdict`, and only when request id and lease id were supplied.
- Otherwise: act as producer. Run `.ai/bin/ai loop submit --source-agent claude --target-agent codex --role worker --priority P1 --interval-seconds 300 --text "$ARGUMENTS" --json`.

Orchestrator rules:
- If claim returns null, reply exactly: `loop queue empty`.
- Delegate maker and reviewer work to separate subagents when available.
- Use request `rubric` and `checklist` as the completion contract.
- Record reviewer output with `ai loop verdict`.
- Complete only after a passing verdict; distill reusable verified learning after complete when useful.

Final reply must be one short Korean status line.
