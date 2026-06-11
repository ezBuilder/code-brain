---
name: "cb-loop-producer"
description: "Code Brain loop producer - create a file-backed task for another agent."
---

# cb-loop-producer

Use this skill when the user wants to hand work from this agent to another agent through Code Brain.

## Command Template

Run:
`.ai/bin/ai loop submit --source-agent antigravity --target-agent codex --role worker --priority P1 --interval-seconds 300 --text "<instruction>" --json`

Then return only:
`loop submitted: {id} -> {path}`

Rules:
- If the instruction is missing, ask for it in one sentence.
- Do not claim, complete, fail, commit, merge, or push.
