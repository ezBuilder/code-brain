---
name: "cb-loop-orchestrator"
description: "Code Brain loop orchestrator - claim a task, delegate maker/reviewer work, and record result."
---

# cb-loop-orchestrator

Use this skill when the user wants this agent to act as the loop worker/orchestrator.

## Command Template

Run:
`.ai/bin/ai loop claim --orchestrator-id antigravity-loop --agent antigravity --json`

If `request` is null, reply exactly:
`loop queue empty`

Otherwise:
- Act as orchestrator, not sole implementer.
- Delegate maker work and reviewer work to separate subagents when available.
- Use the request `rubric` and `checklist` as the completion contract.
- Record reviewer output with `.ai/bin/ai loop verdict --request-id <id> --lease-id <lease_id> --reviewer <name> --verdict pass|fail|blocked --summary "<review summary>" --json`.
- Do not auto-merge or push.
- Verify locally.
- Finish only after a passing verdict with `.ai/bin/ai loop complete --request-id <id> --lease-id <lease_id> --summary "<short summary>" --json`, or `.ai/bin/ai loop fail ...` for real blockers.
- After complete, distill verified reusable learning with `.ai/bin/ai loop distill --request-id <id> --text "<lesson>" --json` when there is a durable lesson. Failures can be distilled too (post-mortem). If it returns `ok:false` with `reason:potential_contradiction`, review the listed `conflicts` against your lesson; only re-run with `--force` once you confirm it does not contradict an existing decision.

Final reply: one short Korean status line.
