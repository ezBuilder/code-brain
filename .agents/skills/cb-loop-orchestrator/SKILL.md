---
name: "cb-loop-orchestrator"
description: "Code Brain loop orchestrator - inspect loopd, dispatch once if needed, and process one assigned task. Never poll the queue yourself."
---

# cb-loop-orchestrator

Use this skill to operate the Code Brain loop. **Do NOT poll the queue with the LLM** — that
wastes tokens. The deterministic `code-brain-loopd` watches the queue and wakes warm workers
when work arrives; an empty queue costs zero tokens.

## Inspect, do not poll

1. `.ai/bin/ai loopd status --json` — see queue counts and worker states.
2. If there are pending requests but nothing is being dispatched, run one deterministic tick:
   `.ai/bin/ai loopd dispatch-once --json` (no LLM; assigns pending work to an idle worker).
3. `.ai/bin/ai loopd recover --json` — recover expired leases / flag stale workers.
4. Never write a "claim every 30s" loop. If no work exists, stop — loopd will dispatch when it does.

`.ai/bin/ai loop claim` is a **one-shot atomic claim**, not a resident loop. Use it only to take
the single request loopd assigned to you.

## Process an assigned task

- Act as orchestrator, not sole implementer.
- If the instruction references a spec document, read the spec first; it is the source of truth.
- Delegate maker and reviewer work to separate subagents when available (maker/checker split).
- Use the request `rubric` and `checklist` as the completion contract.
- Record review with `.ai/bin/ai loop verdict --request-id <id> --lease-id <lease_id> --reviewer <name> --verdict pass|fail|blocked --summary "..." --json`.
- Do not auto-merge or push. Approval-gated work (secrets/auth/billing/prod/destructive) stays blocked.
- Verify locally; finish only after a passing verdict with `.ai/bin/ai loop complete ...`, or `.ai/bin/ai loop fail ...` for real blockers.

## Bring up warm workers (optional)

`.ai/bin/ai loopd up --dry-run` shows the planned isolated tmux workers; drop `--dry-run` to launch.
Each Codex/AGY worker runs under its own profile (isolated HOME/XDG) so auth caches never collide.
