# Code Brain

Repo-local AI agent infrastructure for Claude Code and Codex CLI.

This implementation follows the Claude-authored PRD and MVP implementation plan saved next to this repository:

- `../CLAUDE_AUTHORED_FINAL_PRD.md`
- `../CLAUDE_AUTHORED_MVP_IMPLEMENTATION_PLAN.md`

## Quick Start

```bash
cd code-brain
make env-check
make preflight
make lint
make quick
uv run --project .ai/runtime ai version
uv run --project .ai/runtime ai render --dry-run
uv run --project .ai/runtime ai doctor --strict
printf '{"agent":"codex"}' | uv run --project .ai/runtime ai hook SessionStart --json
uv run --project .ai/runtime ai worker health --json
uv run --project .ai/runtime ai worker status --json
uv run --project .ai/runtime ai index rebuild --json
uv run --project .ai/runtime ai code query "worker IPC" --json
uv run --project .ai/runtime ai obs metrics --json
uv run --project .ai/runtime ai obs health-summary --json
uv run --project .ai/runtime ai diagnostics bundle --dry-run --json
uv run --project .ai/runtime ai migrate --dry-run --json
uv run --project .ai/runtime ai upgrade plan --target-version 0.1.1 --json
uv run --project .ai/runtime ai report status --json
uv run --project .ai/runtime ai report release-gate-summary --json
uv run --project .ai/runtime ai session start --agent codex --json
```

## Full Local Verification

```bash
./bootstrap.sh
./scripts/env-check.sh
./scripts/preflight.sh --check-only --json
./scripts/lint.sh
./scripts/smoke.sh
./scripts/docs-check.sh
./scripts/package.sh
./scripts/verify-artifacts.sh dist/code-brain-0.1.0.tar.gz
./scripts/install-check.sh
./scripts/reproducibility-check.sh
./scripts/artifact-tamper-check.sh
./scripts/rollback-drill.sh
./scripts/bootstrap-idempotency.sh
./scripts/release-gate.sh
make lint
make env-check
make preflight
make release-gate
```

`scripts/smoke.sh` copies the repository to a temporary directory before running write-heavy flows such as queue, trust, inbox, notify, diagnostics bundle, and upgrade rollback. The working tree stays clean.
`bootstrap.sh` starts with the same environment and fresh-clone preflight checks used by the release gate, then renders with `--dry-run` under CI/GitHub Actions.
`scripts/bootstrap-idempotency.sh` runs bootstrap twice in a temporary git copy and fails if tracked source or the generated manifest changes.
`bootstrap.ps1` follows the same CI dry-run render policy for PowerShell operators.
`scripts/docs-check.sh` verifies the operator runbook commands and CI write-denial behavior.
`scripts/verify-artifacts.sh` verifies release checksum, manifest, SBOM, provenance, and release notes without executing package code.
`scripts/reproducibility-check.sh` rebuilds the package into a temporary directory and fails if the archive SHA-256 differs.
`scripts/artifact-tamper-check.sh` verifies that corrupted checksum, manifest, SBOM, provenance, and release notes artifacts are rejected.
`scripts/lockfile-check.sh` verifies `.ai/runtime/uv.lock` drift and prints the `uv lock --project .ai/runtime` remediation when stale.
`ai session start` is the normal Mac/VPS entrypoint after pulling from GitHub; it rebuilds missing or stale local cache, records `SessionStart`, and returns doctor status.
`Makefile` provides operator shortcuts such as `make env-check`, `make preflight`, `make lockfile-check`, `make lock-check`, `make session-start`, `make lint`, `make quick`, `make package`, `make verify-artifacts`, and `make release-gate`.
Use `make clean-cache` for ignored runtime cache files, `make clean-artifacts` for `dist/`, and `make clean-all` for cache, virtualenv, and release artifacts.
GitHub Actions uses the same Makefile targets as local release verification.
`.github/workflows/release-gate.yml` runs the full local gate with read-only repository permissions and uploads `dist/release-gate.summary.json` plus release artifacts for review.
`RELEASE_GATE_SUMMARY_SCHEMA_VERSION` locks the CI summary contract so silent field drift fails generation or cross-OS parity comparison.
Release summary schema v2 includes `dep_advisory.finding_count`, `mode`, `generated_at`, and `skipped` from the advisory artifact.
Dependency vulnerability scanning is advisory-only: `scripts/dep-advisory.sh` writes `dist/dep-advisory.json`, never fails the release gate on findings, and records offline/tool skips explicitly.

## Operations

Use `OPERATIONS.md` as the handoff runbook for daily health checks, queue recovery, trust setup, diagnostics bundles, upgrades, rollback, and troubleshooting.
Use `PRODUCTION_HARDENING_BACKLOG.md` as the dense remaining-work register for commercial hardening rounds.

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
| Worker IPC | `ai worker health/status/stop --force --json` | local envelope validation and singleton lock recovery |
| Hooks | `ai hook <HookName> --json` with JSON stdin | fast-path, redacted, append-only outside CI |
| Session | `ai session start --agent ... --json` | GitHub-baseline Mac/VPS startup with stale index rebuild + SessionStart hook + doctor summary |
| Memory | `ai memory append-event` | append-only JSONL |
| Audit | `ai audit append --action ...` | yearly audit JSONL + audit index + hash-chain verification |
| Search | `ai index rebuild`, `ai code query` | single `.ai/cache/code.sqlite` with FTS5 |
| MCP | `ai mcp` / `ai mcp --once-json ...` | read tools and rebuild request over JSON-RPC |
| Queue | `ai queue enqueue/lease/complete/fail/status` | P0-P3 file queue with lease and dead-letter |
| Trust | `ai trust init/list/revoke` | local age-like identity and tracked machine public record |
| Secrets | `ai secrets status` | key source status without exposing plaintext |
| Inbox | `ai inbox request/list/approve/reject` | narrow 5-gate approval records, redacted |
| Notify | `ai notify enqueue` | P3 outbound adapter jobs, no hot-path network |
| Observability | `ai obs log/metrics/slo/health-summary` | local JSONL logs, metrics, SLO check, and read-only health rollup |
| Diagnostics | `ai diagnostics bundle/prune` | redacted local bundle under `.ai/cache/diagnostics` |
| Release | `ai migrate`, `ai upgrade plan/apply/rollback` | idempotent migration, bootstrap, and local rollback backups |
| Package | `scripts/package.sh`, `scripts/install-check.sh`, `scripts/reproducibility-check.sh` | deterministic tarball + checksum + manifest + SBOM + provenance + release notes + bash/PowerShell install verification |
| Advisory | `scripts/dep-advisory.sh` | read-only dependency vulnerability advisory at `dist/dep-advisory.json` |
| Report | `ai report status/release-notes/release-gate-summary` | release state, artifact integrity, CI summary, and generated notes |

## Release Gate

Before tagging a release:

```bash
./bootstrap.sh
./scripts/env-check.sh
./scripts/lockfile-check.sh
uv lock --check --project .ai/runtime
./scripts/lint.sh
./scripts/smoke.sh
./scripts/docs-check.sh
./scripts/package.sh
./scripts/verify-artifacts.sh dist/code-brain-0.1.0.tar.gz
./scripts/install-check.sh
./scripts/artifact-tamper-check.sh
./scripts/release-gate.sh
make lint
make env-check
make lockfile-check
make lock-check
make release-gate
git status --short
```

Expected result: tests pass, `ai doctor --strict` is green, smoke completes in a temporary copy, and `git status --short` is empty.
