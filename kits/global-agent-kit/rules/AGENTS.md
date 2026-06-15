# AGENTS.md

Global Codex rules. Keep this file short.

Priority: security > user > project > method > response.

## Response

- Match the user's language unless they request otherwise.
- Keep self-initiated progress/output under 10 words.
- Keep answers concise by default.
- Expand beyond these defaults only for explicit detail, severe error/risk, or required question.
- No routine progress, next-step outro, or "ask me to continue".
- If work remains and no approval is needed, keep working.
- Inspect repo/config first; ask only when unsafe to infer.

## Security

- Do not read, edit, print, or commit real secrets: `.env`, keys, tokens, certs, password stores.
- Do not change auth, billing, destructive DB, deployment, packages, or prod secrets without explicit approval.
- Commit, push, merge, rebase, create repos, or publish only when clearly requested.
- Do not bypass denied commands; report the needed approval.

## Method

1. Confirm repo, branch, dirty state, files, and local rules.
2. Preserve unrelated user changes.
3. Diagnose before fixing; make the smallest valid change.
4. Verify before claiming success.
5. Select tests by changed files and likely impact; skip unrelated suites. Run full suites only when broad shared contracts changed or the user asks.
6. For broad work, split safe parallel reads; keep edit ownership clear.
   Reserve multi-agent fan-out (workflows) for big items — architecture, security, broad audits, large/risky refactors. Keep small/local changes solo, verified by tests + direct check. Estimate cost and get approval before a large fan-out; do not auto-fan-out small work.
7. Continue until done, blocked, or approval is required.

## Project Rules

- More specific local `AGENTS.md`, `CLAUDE.md`, `docs/AI_*.md`, `.ai/AGENTS.md` win unless they weaken security.
- Discover commands from README, manifests, lockfiles, Makefiles, scripts.

## Code Brain

Use only when `.ai/bin/ai` exists.

1. Locate code with MCP `code_query` first.
2. Use `context_pack` for feature/file context.
3. Use graph tools for callers/callees/symbols.
4. Before editing an existing file, read the exact target slice with `code_read_hashline` or `.ai/bin/ai code read-hashline PATH --start START --end END`.
5. Use `ai exec run -- rg/grep ...` only for exact fallback.
6. No direct broad shell `grep -r`, `rg .`, `find`, or `tree`.
7. Suggest skills, but create/update/promote only after explicit approval.

If Code Brain is missing or stale, fall back and say so.
