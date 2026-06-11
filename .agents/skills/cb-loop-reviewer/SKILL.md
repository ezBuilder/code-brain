---
name: "cb-loop-reviewer"
description: "Code Brain loop reviewer - review only, without claiming or completing loop tasks."
---

# cb-loop-reviewer

Use this skill when the user wants review-only validation of loop work.

Rules:
- Review only. Do not run `ai loop claim`, `ai loop complete`, or `ai loop fail`.
- The only loop write allowed is `ai loop verdict`, and only when the orchestrator gave you request id and lease id.
- Do not commit, merge, push, or edit files.
- Prioritize correctness, security, regressions, missing tests, and install friction.
- Report findings first with file/line references when available.
- If no issues are found, say that clearly and mention remaining test gaps.
