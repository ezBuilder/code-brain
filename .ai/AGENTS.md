# Code Brain Agent Contract

This repository uses `.ai/` as the single repo-local source for AI agent context, memory, generated metadata, trust, and runtime tooling.

## Branching Policy (hard rule)

- **`develop` is the default working branch.** Every commit, every push, every PR target must be `develop` unless the user explicitly requests otherwise.
- **Never commit or push to `main`.** `main` only advances when the user gives an explicit "merge to main" / "main에 머지" / "main 반영" instruction. No exceptions for "small fixes", "trivial typo", "just one line" — still wait for the explicit request.
- **Do not create new branches** (`feature/*`, `fix/*`, `codex/*`, etc.) unless the user asks. Work directly on `develop`.
- The GitHub repository default branch is `develop`. Local checkouts must mirror this — agents should `git switch develop` at session start if found on another branch and no work is in flight.
- When the user requests a merge to main: fast-forward `main` to `develop`, push, then `git switch develop` back immediately.

## Plan-First Policy

- New feature, refactor, or any change spanning >1 file → enter Plan mode first (or spawn `Plan` subagent), present the plan, wait for user approval before editing.
- Single-file fix, typo, comment update, dependency bump, or work the user explicitly scopes ("just change X") → skip planning.
- A plan must list: files to touch, the contract change, the rollback path. No "and then we'll see".
- Plan approval covers only the scope written down. Discovering extra work → stop and ask, do not silently expand.

## Session Scope (one job per session)

- One feature, one fix, or one investigation per session. When the topic shifts, `/clear`; when context bloats, `/compact`.
- Mixing unrelated work in one session pollutes the audit log, the resume snapshot, and downstream skill/precall recommendations — they get trained on noise.
- If the user introduces a second task mid-session, ask whether to finish current scope first or branch into a new session. Do not silently start a third thread.

## Memory Trust Boundary

- `record_decision` entries are durable rules — treat like CLAUDE.md hard constraints. Do not contradict them without an explicit override decision.
- `record_todo` and `append_session_note` are hints. Re-verify before acting in a new session; they can be stale, partial, or written by a confused prior agent.
- Claude/Codex product-level memory (chat history summarization) captures user preferences, not code rules. Do not encode build/test/branching policy there — fix it in `CLAUDE.md`, `.ai/AGENTS.md`, or a `record_decision`.
- When memory disagrees with the working tree, the working tree wins. Open a fresh decision rather than acting on stale recall.

## Subagent Routing

- File location / "where is X defined?" / "which files reference Y?" → `Explore` (read-only, cheaper, protects main context).
- Multi-step design, impact analysis, or migration sketch → `Plan`.
- Independent parallel research (CI state + tests + docs + open PRs) → one message with multiple `Agent` calls.
- Single-file Read or one Grep that resolves the question → no subagent; the overhead loses.
- Hand the subagent a self-contained brief: goal, what's been ruled out, expected output shape, length cap.

## Search Routing (Token Cost)

- **Code search/discovery (indexed)**: prefer MCP `code_query` / `context_pack` over `Bash grep`/`rg`. BM25 returns top-5 snippets (~2 KB) vs full grep dumps (50–500 KB).
- **Exact code reading before edits**: after `code_query` locates the relevant file, prefer MCP `code_read_hashline` for the smallest needed slice. It returns `line+sha12|content` anchors so agents can detect stale context before applying edits. CLI fallback: `ai code read-hashline <path> --start N --end M`; verify captured anchors with `ai code verify-hashline <path>` when the file may have changed.
- **Large-codebase orientation**: for broad or vague tasks, use `ai code map --json` first to see top-level areas, local AGENTS/CLAUDE files, and scoped test/build commands. Start in the narrowest matching subdirectory; do not load root-wide context when a subdirectory map is enough.
- **Shell command execution with potentially long output**: use MCP `sandbox_execute` (or CLI `ai exec run -- <cmd>`) instead of running long-output commands directly via Bash. The sandbox returns a short summary (`exec_id`, total bytes/lines, first 30 + last 5 lines) and stores the full output. Fetch specific ranges with MCP `sandbox_fetch` (`exec_id`, `line_start`, `line_end` or `grep_pattern`).
- Use `grep`/`rg` directly only as a fallback when MCP is unavailable, when the index is known stale (`ai obs search` exits 13), or for trivial single-file grep.
- The same routing applies inside hook `additionalContext` injection: SessionStart and UserPromptSubmit hooks remind agents of this preference and surface a prior-session resume snapshot when present.
- Memory queries (decisions, todos, prior session narrative) go through MCP `memory_query` / `context_pack`. Do not re-implement memory recall via shell tools.

**Auto-routing (when hooks are registered)**: with `.claude/settings.json` (Claude Code) or `.codex/hooks.json` (Codex CLI) registered, the `PreToolUse` hook intercepts shell calls that match long-output patterns (`grep -r`, `rg`, `find`, `tree`, `ack`, `ag`, `git grep`, and shell-wrapper/compound forms) and *blocks* them with a deny reason that points the agent to `mcp__code-brain__sandbox_execute` or `ai exec run -- <original>`. Single-file non-recursive `grep`, `| wc`, and stdout-to-`/dev/null` forms are allowed through; `| head`/`tail` is still blocked for broad searches. Operators can disable auto-routing by removing the `PreToolUse` block from `.claude/settings.json` or `.codex/hooks.json`.

**Codex runtime fallback**: some Codex Desktop/API sessions may read `AGENTS.md` and expose MCP tools without firing `.codex/hooks.json` automatically. In that case, agents must still apply the same routing manually: use Code Brain MCP first, use `.ai/bin/ai ...` CLI fallback second, and avoid broad shell search/output dumps unless Code Brain is unavailable or stale.

## Cross-Session Memory (proactive logging)

Code Brain's main value is *cross-session context sharing* — but it only works if decisions, todos, and session milestones get *recorded* into `.ai/memory/`. When you (the agent) operate within a Code Brain project, log proactively via these MCP tools (or the equivalent CLI):

- **`mcp__code-brain__record_decision(text, tags?, source?)`** — call whenever the user *decides*, *locks*, *agrees on*, or *rejects* something architectural, scope-related, or policy-level. Examples: "이걸로 가자", "이건 빼자", "X를 default로", "Postgres 대신 SQLite". Keep `text` ≤ 200 chars, one decision per call. Append-only.
- **`mcp__code-brain__record_todo(title, owner?, tags?, source?)`** — call when the user mentions a future task, when you defer work, or when a known follow-up is articulated. Examples: "TODO: refactor X", "다음 라운드에 처리", "당장은 보류". Title ≤ 200 chars.
- **`mcp__code-brain__close_todo(match, status?, reason?)`** — when an earlier todo gets completed, mark it closed. `match` is the todo id or a unique title substring.
- **`mcp__code-brain__append_session_note(text)`** — append a short milestone line to `session-current.md` (visible to the next session via SessionStart hook). Examples: "Round 92 PreToolUse 차단 검증 완료", "navio 재배포 ok".

Why proactive logging matters:
- The next session's `SessionStart` hook auto-injects the last 5 decisions, last 5 open todos, last 8 lines of session-current.md, and the prior-session resume snapshot including the latest session tail into `additionalContext`. Session-start injection has a larger default budget than prompt-start injection (`AI_SESSION_START_MAX_BYTES`, default 12 KB) because it is the high-value recovery point.
- If you don't log, the next session sees an empty memory layer and Code Brain's central feature delivers nothing.
- Hook injection bytes/call is the leading indicator: when low, log more.

CLI equivalents (operator-side):
```
ai memory decision add --text "Adopt MCP code_query as default search" --tag policy
ai memory todo add --title "Verify cross-OS summary parity stays green for v0.2" --owner ops
ai memory todo close --match "cross-OS summary parity"
ai memory session append --text "Round 93 memory layer wired"
```

All four operations are write-class: rejected in CI per existing `WRITE_COMMANDS` policy.

## Skill Recommendation (slash command synthesis)

Code Brain proposes per-project slash commands from accumulated memory. Flow is *recommend → review → accept* — never auto-install. Surface candidates to the user and wait for approval. Drafts containing prompt-injection patterns auto-reject during `accept`.

Full mechanics (CLI, MCP, catalog format, safety): see `.ai/policies/skill-recommendation.md`.

## Hook Event Coverage

Code Brain registers Claude Code hooks: PreToolUse, PostToolUse, SessionStart, UserPromptSubmit, Stop, SubagentStop, **PreCompact, PostCompact, SessionEnd, Notification**. Codex hooks: PreToolUse, PostToolUse, SessionStart, UserPromptSubmit, Stop, SubagentStop, **PreCompact, PostCompact, PermissionRequest**.

PreCompact and SessionEnd force-write a session-resume snapshot before the session boundary so cross-session memory survives `/compact` and `/clear`. Notification and PermissionRequest emit observation-only audit entries (no blocking yet).

`install-into.sh` ensures `~/.codex/config.toml` (or `<repo>/.codex/config.toml`) sets `[features].hooks = true` idempotently — without disturbing other user-defined keys in that section. The deprecated `codex_hooks` key, if present, is migrated to `hooks`.

Hook responses follow the Claude Code spec via `hookSpecificOutput.{hookEventName, additionalContext, permissionDecision}`. Top-level `additionalContext` is preserved for backward compat.

## Precall Rule Recommendation

Code Brain mines PreToolUse Bash invocations and proposes precall rules that route or block matching commands. Lifecycle `pending → dry_run → active`. Active rules never override built-in `LONG_OUTPUT_BINARIES` interception. User overrides ≥3 auto-disable an active rule.

Full mechanics (CLI, MCP, regex safety probe): see `.ai/policies/precall-rules.md`.

## Hard Constraints

- The worker is the only source-of-truth writer. All persistent writes must go through worker IPC after M2.
- No hook or MCP hot path may call the network.
- Embeddings, remote LLM calls, reranking, and external notification channels are off by default.
- CI is read-only. Write commands are rejected at parse time before worker contact.
- Tracked source may not contain plaintext secrets.
- Only SOPS+age ciphertext may be tracked under `.ai/secrets/*.enc.yaml`.
- `.ai/cache/code.sqlite` is the single cache database.
- `.ai/generated/manifest.json` is the single metadata owner.
- `--no-redact` may affect only local stdout with interactive TTY, `--yes`, and audit.
- MCP, external channels, and diagnostics are always redacted.

## MVP Status

This scaffold implements M0-M1 foundations:

- repo layout
- uv runtime
- `ai` CLI
- `ai render`
- `ai doctor`
- CI read-only command rejection
- manifest generation
- basic secret scanning and policy checks
