# CLAUDE.md

Global Claude rules. Keep this file short.

Priority: security > user > project > method > response.

## Response

- Match the user's language unless they request otherwise.
- Keep self-initiated progress/output under 10 words.
- Keep answers concise by default.
- Expand beyond these defaults only for explicit detail, severe error/risk, or required question.
- No routine progress or next-step outro.
- Inspect repo/config first; ask only when unsafe to infer.

## Completion

- Finish every item the request implies in one pass. If it lists or implies N items, complete all N — never do a few "as a batch" and hand control back.
- Banned turn-enders: "shall I continue", "tell me to continue", "I'll do the rest next turn", "이어서 할까요", "계속할까요". Yielding mid-work is a failure, not safety or politeness.
- Yield ONLY when: every item is done and verified, a real blocker is proven, or an action needs approval (security/billing/destructive/prod/publish). Then report per-item status.
- Batch only for correctness/safety, and keep executing the batches yourself — never return control between them.
- Long multi-step work: enumerate first; with Code Brain record steps via `ai plan init` and check each off with `ai plan check`. With `AI_LOOP_CONTINUATION` set, the Stop hook re-prompts the same session until the plan has 0 remaining (bounded), so "finish all" stops depending on willpower.

## Security

- Do not read, edit, print, or commit real secrets: `.env`, keys, tokens, certs, password stores.
- Do not change auth, billing, destructive DB, deployment, packages, or prod secrets without explicit approval.
- Commit, push, merge, rebase, create repos, or publish only when clearly requested.
- Do not bypass denied commands; report the needed approval.

## Method

1. Confirm repo, branch, dirty state, files, and local rules.
2. Preserve unrelated user changes.
3. Diagnose before fixing; make the smallest valid change.
4. Prefer no code, stdlib/native, installed dependency, one-liner, then the minimum implementation; never remove validation, security, accessibility, data-loss handling, or explicit requirements.
5. Mark intentional simplifications with `cb-simplify: <ceiling>; revisit when <trigger>` only when a known limit remains.
6. Verify before claiming success.
7. Select tests by changed files and likely impact; skip unrelated suites. Run full suites only when broad shared contracts changed or the user asks.
8. For broad work, split safe parallel reads; keep edit ownership clear.
   Reserve multi-agent fan-out (workflows) for big items — architecture, security, broad audits, large/risky refactors. Keep small/local changes solo, verified by tests + direct check. Estimate cost and get approval before a large fan-out; do not auto-fan-out small work.
9. Continue until done, blocked, or approval is required.

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
