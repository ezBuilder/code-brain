---
name: "cb-loop-orchestrator"
description: "Code Brain loop orchestrator - poll the queue continuously, claim tasks as they arrive, delegate maker/reviewer work, and record results."
---

# cb-loop-orchestrator

Use this skill when the user wants this agent to act as the resident loop worker/orchestrator.

This is a RESIDENT loop, not a one-shot check. An empty queue means "keep polling", not "done". Only stop when the user tells you to stop.

## Poll for work

Wait for the next task with a bounded blocking poll (~10 min per round, 30s interval):

`bash -c 'r=""; for i in $(seq 1 20); do r="$(.ai/bin/ai loop claim --orchestrator-id <orchestrator-id> --agent <this-agent> --json)"; echo "$r" | grep -Eq "\"request\": ?null" || break; sleep 30; done; printf "%s\n" "$r"'`

- If the poll returns a non-null `request`: process it (next section), then immediately poll again.
- If it still returns `"request": null` after the round: report `loop queue empty - polling continues` and start the next poll round. Do NOT end the session over an empty queue. If the host cannot keep a long-running turn alive, re-arm yourself with the host's scheduler (Claude Code: `/loop` or a scheduled wakeup; otherwise re-run this poll command) instead of stopping.

## Process a claimed task

- Act as orchestrator, not sole implementer.
- If the task instruction references a spec document, read the spec first; it is the source of truth.
- Delegate maker work and reviewer work to separate subagents when available.
- Use the request `rubric` and `checklist` as the completion contract.
- Record reviewer output with `.ai/bin/ai loop verdict --request-id <id> --lease-id <lease_id> --reviewer <name> --verdict pass|fail|blocked --summary "<review summary>" --json`.
- Do not auto-merge or push.
- Verify locally.
- Finish only after a passing verdict with `.ai/bin/ai loop complete --request-id <id> --lease-id <lease_id> --summary "<short summary>" --json`, or `.ai/bin/ai loop fail ...` for real blockers.
- After complete, distill verified reusable learning with `.ai/bin/ai loop distill --request-id <id> --text "<lesson>" --json` when there is a durable lesson. Failures can be distilled too (post-mortem). If it returns `ok:false` with `reason:potential_contradiction`, review the listed `conflicts` against your lesson; only re-run with `--force` once you confirm it does not contradict an existing decision.
- After completing (or failing) a task, return to the poll loop.

Status updates: one short Korean line per event (claimed / verdict / completed / queue empty, polling continues).
