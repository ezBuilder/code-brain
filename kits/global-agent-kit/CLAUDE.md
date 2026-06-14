# CLAUDE.md

Scope: global-agent-kit for Claude/Codex rules and installers.

## Targets

- Claude rules: `rules/CLAUDE.md`
- Codex rules: `rules/AGENTS.md`
- Installer: `install.sh`
- Checks: `scripts/validate.sh`, `scripts/doctor.sh`, `scripts/harness.sh`
- Deep policy: `docs/AI_*.md`
- Indexed docs: `AI_ARCHITECTURE.md`, `AI_CONTEXT.md`, `AI_HOOKS.md`, `AI_SECURITY.md`, `AI_SUBAGENTS.md`, `AI_TESTING.md`, `AI_INTEGRATIONS.md`, `AI_DEV_LOOP.md`, `AI_RESEARCH.md`, `AI_TOKEN_OPTIMIZATION.md`

## Rules

- Keep global rules short; move detail to `docs/`.
- Preserve install safety: dry-run, backups, clear failures.
- Put Claude-only behavior in `rules/CLAUDE.md` or `.claude/`.
- Put Codex-only behavior in `rules/AGENTS.md`.
- Keep Code Brain optional; never make it a global dependency.
- Do not touch user settings, auth files, commits, pushes, publishes, or deploys unless explicitly requested.
- Verify with `./scripts/validate.sh` or `make validate`.
