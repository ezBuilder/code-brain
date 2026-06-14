# AGENTS.md

Global Codex rules. Keep this file short.

Priority: security > user > project > method > response.

## Response

- Reply Korean.
- Self-initiated progress/output: <=10 Korean chars.
- Answers to user questions: <=50 Korean chars.
- Ignore length caps when the user explicitly requests detail.
- Expand only for explicit detail, severe error/risk, or required question.
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
5. For broad work, split safe parallel reads; keep edit ownership clear.
6. Continue until done, blocked, or approval is required.

## Project Rules

- More specific local `AGENTS.md`, `CLAUDE.md`, `docs/AI_*.md`, `.ai/AGENTS.md` win unless they weaken security.
- Discover commands from README, manifests, lockfiles, Makefiles, scripts.

## Code Brain

Use only when `.ai/bin/ai` exists.

1. Locate code with MCP `code_query` first.
2. Use `context_pack` for feature/file context.
3. Use graph tools for callers/callees/symbols.
4. Read edit slices with `code_read_hashline` or `.ai/bin/ai code read-hashline`.
5. Use `ai exec run -- rg/grep ...` only for exact fallback.
6. No direct broad shell `grep -r`, `rg .`, `find`, or `tree`.
7. Suggest skills, but create/update/promote only after explicit approval.

If Code Brain is missing or stale, fall back and say so.
