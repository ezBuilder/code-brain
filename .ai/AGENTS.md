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

## Subagent Routing

- File location / "where is X defined?" / "which files reference Y?" → `Explore` (read-only, cheaper, protects main context).
- Multi-step design, impact analysis, or migration sketch → `Plan`.
- Independent parallel research (CI state + tests + docs + open PRs) → one message with multiple `Agent` calls.
- Single-file Read or one Grep that resolves the question → no subagent; the overhead loses.
- Hand the subagent a self-contained brief: goal, what's been ruled out, expected output shape, length cap.

## Search Routing (Token Cost)

- **Code search/discovery (indexed)**: prefer MCP `code_query` / `context_pack` over `Bash grep`/`rg`. BM25 returns top-5 snippets (~2 KB) vs full grep dumps (50–500 KB).
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

Code Brain mines accumulated memory (decisions, todos, audit, session notes; optionally project-filtered Claude/Codex global memory) and proposes per-project slash commands. The flow is *recommend → review → accept*; never auto-install.

- **Proactive surfacing**: the `SessionStart` hook may inject a short "Skill recommendations available" block when local signals cross the threshold. The agent must show those candidates to the user and ask for approval before accepting. Do not silently install, reject, or promote.
- **List candidates**: `ai recommend skills [--limit N] [--no-global] [--min-signal K] [--json]` — local-only, no LLM/network, no install. It may persist pending catalog entries so accept/reject can target stable ids. Returns `{candidates: [{id, slug, description, body, evidence}, ...]}`.
- **Install one**: `ai recommend skills accept <id>` — writes `.claude/commands/<slug>.md` and `.codex/prompts/<slug>.md` with frontmatter `managed-by: code-brain` + `body-sha256` for drift tracking. Write-class.
- **Dismiss**: `ai recommend skills reject <id>` — kept in catalog with status `rejected` so it is never re-suggested.
- **List installed**: `ai skills list [--json]`
- **Remove**: `ai skills uninstall <slug> [--force]` — refuses if disk content drifted from recorded sha (user edits) unless `--force`.

MCP equivalents: `recommend_skills`, `recommend_skills_accept`, `recommend_skills_reject`, `skills_list`, `skills_uninstall`.

Catalog persists at `.ai/skills/catalog.jsonl` (append-only, redacted). Excluded from FTS5 indexing via `SKIP_PATH_PREFIXES`.

Danger patterns (`<system-reminder>`, `Ignore previous instructions`, etc.) in draft bodies cause auto-rejection during `accept` — global-memory inputs cannot inject prompts via this surface.

## Hook Event Coverage

Code Brain registers Claude Code hooks: PreToolUse, PostToolUse, SessionStart, UserPromptSubmit, Stop, SubagentStop, **PreCompact, PostCompact, SessionEnd, Notification**. Codex hooks: PreToolUse, PostToolUse, SessionStart, UserPromptSubmit, Stop, SubagentStop, **PreCompact, PostCompact, PermissionRequest**.

PreCompact and SessionEnd force-write a session-resume snapshot before the session boundary so cross-session memory survives `/compact` and `/clear`. Notification and PermissionRequest emit observation-only audit entries (no blocking yet).

`install-into.sh` ensures `~/.codex/config.toml` (or `<repo>/.codex/config.toml`) sets `[features].hooks = true` idempotently — without disturbing other user-defined keys in that section. The deprecated `codex_hooks` key, if present, is migrated to `hooks`.

Hook responses follow the Claude Code spec via `hookSpecificOutput.{hookEventName, additionalContext, permissionDecision}`. Top-level `additionalContext` is preserved for backward compat.

## Precall Rule Recommendation

In addition to slash-command recommendations, Code Brain mines accumulated PreToolUse Bash invocations (audit log + optional transcripts) and proposes user-defined precall rules — patterns that should be routed to Code Brain's sandbox or otherwise blocked.

Lifecycle: `pending → dry_run → active`. Active rules block matching commands. User overrides ≥3 within an active rule's lifetime auto-disable it.

- `ai precall recommend [--limit N] [--min-signal K]` — read-only, surfaces candidates
- `ai precall accept <id>` — promote pending → dry_run (passes safety probe + regex compile)
- `ai precall activate <id> [--force]` — promote dry_run → active (refuses if observed < required, default 5)
- `ai precall reject <id>` / `ai precall disable <id>` — terminal states
- `ai precall list`

Safety: anchored regex required (`^...`), catch-all rejected, sanity probe matches against a whitelist (`echo ok`, `ls`, `pwd`, `git status`, `true`, `cat README.md`) to refuse over-broad patterns. Active rules never override built-in `LONG_OUTPUT_BINARIES` interception or hatch detection.

MCP equivalents: `precall_recommend`, `precall_list`, `precall_accept`, `precall_activate`, `precall_reject`, `precall_disable`.

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
