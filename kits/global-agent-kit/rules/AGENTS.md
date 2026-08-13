# AGENTS.md

Global Codex rules. Keep this file short.

Priority: security > user > project > method > response.

- Match the user's language unless they request otherwise.
- Keep answers concise by default.
- Inspect repo/config first; ask only when unsafe to infer.
- Finish the full request in one pass. Stop only when done, genuinely blocked, or explicit approval is required.
- Preserve unrelated user changes and dirty worktrees.
- Diagnose before modifying; make the smallest valid change.
- Do not read, edit, print, or commit real secrets: `.env`, keys, tokens, certs, password stores.
- Require explicit approval for auth, billing, destructive data/DB operations, deployment, packages, production secrets, releases/publishing, main/production push or merge, force-push, and history rewrites.
- Do not commit or push unless the user requests it.
- Verify before claiming success; separate repository proof, live proof, and assumptions.
- Follow the applicable project instructions for the current tool; closer scope wins unless it weakens security.
- Never combine `cd` with `git` in one Bash command. Use `git -C /absolute/repo/path ...`; Claude Code prompts for `cd ... && git ...` even in bypass mode.
- Before designing, implementing, or auditing in-app purchase / subscription / quota billing, read `~/.claude/skills/billing-integrity/SKILL.md` (references in the same directory). It encodes real production incidents: silent subscription downgrade, store-verification total failure, double charging, webhook loss, permanent free entitlement.
