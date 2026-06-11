---
description: Code Brain loop producer - create a file-backed task for another agent.
argument-hint: "<goal/instruction>"
---

Take `$ARGUMENTS` as the instruction. If empty, ask the user for the instruction in one sentence.

Run `.ai/bin/ai loop submit --source-agent claude --target-agent codex --role worker --priority P1 --interval-seconds 300 --text "$ARGUMENTS" --json`.

Return only:
`loop submitted: {id} -> {path}`
