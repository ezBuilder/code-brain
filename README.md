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
printf '{"agent":"codex"}' | uv run --project .ai/runtime ai hook SessionStart --json
uv run --project .ai/runtime ai worker health --json
uv run --project .ai/runtime ai index rebuild --json
uv run --project .ai/runtime ai code query "worker IPC" --json
printf '{"task":"rebuild"}' | uv run --project .ai/runtime ai queue enqueue --priority P2 --kind index --json
uv run --project .ai/runtime ai trust init --name "$(hostname)" --json
uv run --project .ai/runtime ai render
printf '{"reason":"need outbound"}' | uv run --project .ai/runtime ai inbox request --gate remote_enable --summary "Enable outbound adapter" --json
printf '{"summary":"hello"}' | uv run --project .ai/runtime ai notify enqueue --channel telegram --json
```

## Locked Rules

- `.ai/` is the single repo-local source.
- Hooks and MCP hot paths do not perform network calls.
- CI is read-only. Write commands are rejected before worker contact.
- Tracked source must not contain plaintext secrets.
- `.ai/cache/code.sqlite` is the single cache database.
- `.ai/generated/manifest.json` owns generated metadata.
- Audit data is append-only and rotates by year.

## Implemented MVP Surface

| Area | Command | Status |
|---|---|---|
| CLI | `ai version`, `ai config show` | working |
| Render | `ai render --dry-run`, `ai render --no-overwrite` | working |
| Doctor | `ai doctor --strict --json` | working |
| Worker IPC | `ai worker health --json` | local envelope validation |
| Hooks | `ai hook <HookName> --json` with JSON stdin | fast-path, redacted, append-only outside CI |
| Memory | `ai memory append-event` | append-only JSONL |
| Audit | `ai audit append --action ...` | yearly audit JSONL + audit index |
| Search | `ai index rebuild`, `ai code query` | single `.ai/cache/code.sqlite` with FTS5 |
| MCP | `ai mcp` / `ai mcp --once-json ...` | read tools and rebuild request over JSON-RPC |
| Queue | `ai queue enqueue/lease/complete/fail/status` | P0-P3 file queue with lease and dead-letter |
| Trust | `ai trust init/list/revoke` | local age-like identity and tracked machine public record |
| Secrets | `ai secrets status` | key source status without exposing plaintext |
| Inbox | `ai inbox request/list/approve/reject` | narrow 5-gate approval records, redacted |
| Notify | `ai notify enqueue` | P3 outbound adapter jobs, no hot-path network |
