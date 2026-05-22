# Skill Recommendation — full mechanics

> Summarized in `.ai/AGENTS.md`. This file holds the long form.

Code Brain mines accumulated memory (decisions, todos, audit, session
notes; optionally project-filtered Claude/Codex global memory) and
proposes per-project slash commands. The flow is *recommend → review →
accept*; never auto-install.

## Surfaces

- **Proactive surfacing**: the `SessionStart` hook may inject a short
  "Skill recommendations available" block when local signals cross the
  threshold. The agent must show those candidates to the user and ask
  for approval before accepting. Do not silently install, reject, or
  promote.
- **List candidates**: `ai recommend skills [--limit N] [--no-global]
  [--min-signal K] [--json]` — local-only, no LLM/network, no install.
  May persist pending catalog entries so accept/reject can target
  stable ids. Returns
  `{candidates: [{id, slug, description, body, evidence}, ...]}`.
- **Install one**: `ai recommend skills accept <id>` — writes
  `.claude/commands/<slug>.md` and `.codex/prompts/<slug>.md` with
  frontmatter `managed-by: code-brain` + `body-sha256` for drift
  tracking. Write-class.
- **Dismiss**: `ai recommend skills reject <id>` — kept in catalog with
  status `rejected` so it is never re-suggested.
- **List installed**: `ai skills list [--json]`.
- **Remove**: `ai skills uninstall <slug> [--force]` — refuses if disk
  content drifted from recorded sha (user edits) unless `--force`.

MCP equivalents: `recommend_skills`, `recommend_skills_accept`,
`recommend_skills_reject`, `skills_list`, `skills_uninstall`.

## Storage

Catalog persists at `.ai/skills/catalog.jsonl` (append-only, redacted).
Excluded from FTS5 indexing via `SKIP_PATH_PREFIXES`.

## Safety

Danger patterns (`<system-reminder>`, `Ignore previous instructions`,
etc.) in draft bodies cause auto-rejection during `accept` — global-
memory inputs cannot inject prompts via this surface.
