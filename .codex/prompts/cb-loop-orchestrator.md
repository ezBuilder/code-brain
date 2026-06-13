---
description: Code Brain loop orchestrator - claim a task, delegate maker/reviewer work, and record result.
argument-hint: "[orchestrator-id]"
---

Use `$ARGUMENTS` as orchestrator id. If empty, use `codex-loop`.

Do NOT poll the queue in a loop — `code-brain-loopd` watches it and dispatches to warm workers
with zero tokens. First check `.ai/bin/ai loopd status --json`; if work is pending but idle, run
`.ai/bin/ai loopd dispatch-once --json`.

`.ai/bin/ai loop claim` is a one-shot atomic claim. Use it once to take an assigned request:
`.ai/bin/ai loop claim --orchestrator-id "<orchestrator-id>" --agent codex --json`.
If `request` is null, reply exactly: `loop queue empty` (loopd dispatches when work arrives).

Otherwise:
- Act as orchestrator, not sole implementer.
- Delegate maker work and reviewer work to separate subagents when available.
- Use the request `rubric` and `checklist` as the completion contract.
- Record reviewer output with `.ai/bin/ai loop verdict --request-id <id> --lease-id <lease_id> --reviewer <name> --verdict pass|fail|blocked --summary "<review summary>" --json`.
- Do not auto-merge or push.
- Verify locally.
- Finish only after a passing verdict with `.ai/bin/ai loop complete --request-id <id> --lease-id <lease_id> --summary "<short summary>" --json`, or `.ai/bin/ai loop fail ...` for real blockers.
- After complete, distill verified reusable learning with `.ai/bin/ai loop distill --request-id <id> --text "<lesson>" --json` when there is a durable lesson.

Final reply: one short Korean status line.
