# Precall Rule Recommendation — full mechanics

> Summarized in `.ai/AGENTS.md`. This file holds the long form.

In addition to slash-command recommendations, Code Brain mines
accumulated PreToolUse Bash invocations (audit log + optional
transcripts) and proposes user-defined precall rules — patterns that
should be routed to Code Brain's sandbox or otherwise blocked.

## Lifecycle

`pending → dry_run → active`. Active rules block matching commands.
User overrides ≥3 within an active rule's lifetime auto-disable it.

## CLI surface

- `ai precall recommend [--limit N] [--min-signal K]` — read-only,
  surfaces candidates.
- `ai precall accept <id>` — promote pending → dry_run (passes safety
  probe + regex compile).
- `ai precall activate <id> [--force]` — promote dry_run → active
  (refuses if observed < required, default 5).
- `ai precall reject <id>` / `ai precall disable <id>` — terminal
  states.
- `ai precall list`.

MCP equivalents: `precall_recommend`, `precall_list`, `precall_accept`,
`precall_activate`, `precall_reject`, `precall_disable`.

## Safety

Anchored regex required (`^...`), catch-all rejected, sanity probe
matches against a whitelist (`echo ok`, `ls`, `pwd`, `git status`,
`true`, `cat README.md`) to refuse over-broad patterns. Active rules
never override built-in `LONG_OUTPUT_BINARIES` interception or hatch
detection.
