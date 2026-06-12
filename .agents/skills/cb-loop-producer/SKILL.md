---
name: "cb-loop-producer"
description: "Code Brain loop producer - analyze the codebase, write a self-contained work-spec document, then submit a doc-backed task for another agent."
---

# cb-loop-producer

Use this skill when the user wants to hand work from this agent to another agent through Code Brain.

The producer does the thinking; the worker does the building. NEVER forward the user's raw request verbatim into the queue.

## Workflow

1. **Clarify the goal.** If the instruction is missing or too vague to analyze, ask for it in one sentence. Otherwise do not ask — analyze.
2. **Analyze the codebase.** Locate the relevant features, files, and existing patterns (prefer `code_query`/`context_pack`; use parallel subagents for research when the host supports them). Establish current behavior, constraints, and integration points.
3. **Decide one concrete direction.** If multiple viable approaches exist, pick the best and record the rationale in the doc. Do not leave open questions to the worker unless truly user-decidable.
4. **Write the work-spec document** at `.ai/outputs/loop/<task-slug>-spec.md` with these sections:
   - Background & goal (short)
   - Current state (key files with exact paths; how the flows work today)
   - Decided direction & rationale
   - Change plan (file-by-file, concrete)
   - Acceptance criteria (testable checklist)
   - Verification commands
   - Out of scope
   The spec must be self-contained: the worker has no access to this conversation.
5. **Submit the task pointing at the doc** (one checklist flag per acceptance criterion):

   `.ai/bin/ai loop submit --source-agent <this-agent> --target-agent codex --role worker --priority P1 --goal "<one-line goal>" --text "Read the spec at <doc path> and implement it exactly. The acceptance criteria in the spec are the completion contract." --checklist "<criterion 1>" --checklist "<criterion 2>" --rubric "<what the reviewer must focus on>" --json`

6. Reply with exactly two lines:
   - `spec: <doc path>`
   - `loop submitted: {id} -> {path}`

Rules:
- Do not claim, complete, fail, commit, merge, or push.
- Do not submit until the spec file exists on disk.
