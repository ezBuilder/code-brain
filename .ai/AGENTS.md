# Code Brain Agent Contract

This repository uses `.ai/` as the single repo-local source for AI agent context, memory, generated metadata, trust, and runtime tooling.

## Search Routing (Token Cost)

- **Code search/discovery (indexed)**: prefer MCP `code_query` / `context_pack` over `Bash grep`/`rg`. BM25 returns top-5 snippets (~2 KB) vs full grep dumps (50–500 KB).
- **Shell command execution with potentially long output**: use MCP `sandbox_execute` (or CLI `ai exec run -- <cmd>`) instead of running long-output commands directly via Bash. The sandbox returns a short summary (`exec_id`, total bytes/lines, first 30 + last 5 lines) and stores the full output. Fetch specific ranges with MCP `sandbox_fetch` (`exec_id`, `line_start`, `line_end` or `grep_pattern`).
- Use `grep`/`rg` directly only as a fallback when MCP is unavailable, when the index is known stale (`ai obs search` exits 13), or for trivial single-file grep.
- The same routing applies inside hook `additionalContext` injection: SessionStart and UserPromptSubmit hooks remind agents of this preference and surface a prior-session resume snapshot when present.
- Memory queries (decisions, todos, prior session narrative) go through MCP `memory_query` / `context_pack`. Do not re-implement memory recall via shell tools.

**Auto-routing (when hooks are registered)**: with `.claude/settings.json` (Claude Code) or `.codex/hooks.json` (Codex CLI) registered, the `PreToolUse` hook intercepts `Bash` calls that match long-output patterns (`grep -r`, `rg`, `find`, `tree`, `ack`, `ag`) and *blocks* them with a deny reason that points the agent to `mcp__code-brain__sandbox_execute` or `ai exec run -- <original>`. Single-file `grep`, piped-to-`head`/`tail`/`wc`, and `2>/dev/null`-suppressed commands are allowed through. Operators can disable auto-routing by removing the `PreToolUse` block from `.claude/settings.json`.

## Cross-Session Memory (proactive logging)

Code Brain's main value is *cross-session context sharing* — but it only works if decisions, todos, and session milestones get *recorded* into `.ai/memory/`. When you (the agent) operate within a Code Brain project, log proactively via these MCP tools (or the equivalent CLI):

- **`mcp__code-brain__record_decision(text, tags?, source?)`** — call whenever the user *decides*, *locks*, *agrees on*, or *rejects* something architectural, scope-related, or policy-level. Examples: "이걸로 가자", "이건 빼자", "X를 default로", "Postgres 대신 SQLite". Keep `text` ≤ 200 chars, one decision per call. Append-only.
- **`mcp__code-brain__record_todo(title, owner?, tags?, source?)`** — call when the user mentions a future task, when you defer work, or when a known follow-up is articulated. Examples: "TODO: refactor X", "다음 라운드에 처리", "당장은 보류". Title ≤ 200 chars.
- **`mcp__code-brain__close_todo(match, status?, reason?)`** — when an earlier todo gets completed, mark it closed. `match` is the todo id or a unique title substring.
- **`mcp__code-brain__append_session_note(text)`** — append a short milestone line to `session-current.md` (visible to the next session via SessionStart hook). Examples: "Round 92 PreToolUse 차단 검증 완료", "navio 재배포 ok".

Why proactive logging matters:
- The next session's `SessionStart` hook auto-injects the last 5 decisions, last 5 open todos, last 8 lines of session-current.md, and the prior-session resume snapshot into `additionalContext`.
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

