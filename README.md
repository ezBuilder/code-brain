# Code Brain

Repo-local AI agent infrastructure for Claude Code and Codex CLI.

This implementation follows the Claude-authored PRD and MVP implementation plan saved next to this repository:

- `../CLAUDE_AUTHORED_FINAL_PRD.md`
- `../CLAUDE_AUTHORED_MVP_IMPLEMENTATION_PLAN.md`

## Quick Start

```bash
cd code-brain
uv run --project .ai/runtime ai version
uv run --project .ai/runtime ai render --dry-run
uv run --project .ai/runtime ai doctor --strict
```

## Locked Rules

- `.ai/` is the single repo-local source.
- Hooks and MCP hot paths do not perform network calls.
- CI is read-only. Write commands are rejected before worker contact.
- Tracked source must not contain plaintext secrets.
- `.ai/cache/code.sqlite` is the single cache database.
- `.ai/generated/manifest.json` owns generated metadata.
- Audit data is append-only and rotates by year.

