# Operations Runbook

This runbook is for operating Code Brain after handoff. It assumes a repo-local install where `.ai/` is the only source of agent runtime state.

## First Run

```bash
cd code-brain
make env-check
make preflight
make lockfile-check
make lock-check
make lint
make quick
./bootstrap.sh
uv run --project .ai/runtime ai doctor --strict --json
uv run --project .ai/runtime ai report status --json
uv run --project .ai/runtime ai report release-gate-summary --json
uv run --project .ai/runtime ai worker status --json
```

Expected result:

- `doctor.ok` is `true`.
- `release_artifacts.all_present` and `release_artifacts.all_valid` are `true` after packaging.
- `release_ready` is `true` only when doctor is green, release artifacts are present/valid, and provenance matches the current clean git HEAD.
- The release gate fails when `release_ready` or `release_artifacts.all_current` is not `true`.
- `report.status.ok` is `true`.
- `git.status_short` is empty for tracked files.
- Runtime artifacts appear only in ignored paths such as `.ai/cache/`, `.ai/runtime/.venv/`, `.ai/runtime/.pytest_cache/`, `__pycache__/`, and `dist/`.

## Release Gate

Run the full gate before tagging, shipping an archive, or handing a build to another machine:

```bash
./scripts/release-gate.sh
make env-check
make preflight
make lint
make release-gate
uv run --project .ai/runtime ai report release-notes
```

The release gate runs environment checks, fresh-clone preflight, `uv.lock` drift verification, bootstrap, tests, smoke flows in a temporary copy, package creation, install verification, package reproducibility check, rollback drill, bootstrap idempotency drill, doctor, docs examples, and release status reporting. It fails if tracked source becomes dirty.
It starts with `scripts/env-check.sh`, which reports bash, git, make, uv, uv-managed Python, and optional PowerShell status as JSON.
It also starts with `scripts/preflight.sh --check-only`, which verifies repo layout, required tools, Python version, conditional encrypted-secret tooling, conditional Git LFS tooling, and cache permission posture.
It runs `scripts/lockfile-check.sh` before package creation so runtime dependency changes cannot drift from the checked-in lockfile. The script wraps `uv lock --check --project .ai/runtime` and prints the `uv lock --project .ai/runtime` remediation when the lockfile is missing or stale.
It starts with `scripts/lint.sh`, which checks shell syntax, Python compilation, Makefile dry-runs, and PowerShell bootstrap/shim parsing when PowerShell is available.
Direct `bootstrap.sh` runs also start with `scripts/env-check.sh` and `scripts/preflight.sh --check-only`; `bootstrap.sh` and `bootstrap.ps1` render with `--dry-run` under CI/GitHub Actions.
It also runs artifact tamper checks so checksum, manifest, SBOM, provenance, and release notes corruption must be rejected before release.
It runs `scripts/reproducibility-check.sh` after install verification so repeated package builds must produce the same archive SHA-256.
It runs `scripts/bootstrap-idempotency.sh` in a temporary git copy and fails if two consecutive CI-mode bootstrap runs change tracked source or `.ai/generated/manifest.json`.
Use `scripts/verify-artifacts.sh` when you need to validate downloaded release artifacts before running package code.
CI uses the same Makefile targets as local release verification; write-heavy smoke/docs flows run only inside temporary repositories with CI policy explicitly cleared.
`.github/workflows/release-gate.yml` runs the full release gate with read-only repository permissions, verifies CI write rejection, uploads `dist/release-gate.summary.json`, `dist/dep-advisory.json`, plus release artifacts for retention, and uses `summary-observe` with `scripts/summary-parity.py` to compare canonical summary fields across supported CI operating systems.
Release gate summary schema is locked by `RELEASE_GATE_SUMMARY_SCHEMA_VERSION`; `scripts/summary-parity.py` rejects missing, extra, or wrong-version summary fields before comparing cross-OS content. Schema v2 includes `dep_advisory.finding_count`, `mode`, `generated_at`, and `skipped`; parity compares stable advisory fields while ignoring per-run advisory timestamps.

## Install From Archive

Build and verify the archive:

```bash
./scripts/package.sh
./scripts/verify-artifacts.sh dist/code-brain-0.1.0.tar.gz
./scripts/install-check.sh
make package
make verify-artifacts
make install-check
```

Artifact verification checks release files without executing package code:

- archive checksum;
- file manifest hashes when `dist/code-brain-<version>.manifest.json` exists;
- SBOM lockfile and dependency package list when `dist/code-brain-<version>.sbom.json` exists;
- provenance subjects when `dist/code-brain-<version>.provenance.json` exists;
- release notes contents and provenance subject when `dist/code-brain-<version>.release-notes.md` exists.

Install verification then extracts the latest `dist/code-brain-<version>.tar.gz` into a temporary directory and verifies:

- `ai version`
- `ai doctor --strict`
- `.ai/bin/ai`
- `.ai/bin/ai-hook`
- `.ai/bin/ai.ps1` and `.ai/bin/ai-hook.ps1` when PowerShell is available
- runtime tests

## Daily Health Check

```bash
uv run --project .ai/runtime ai doctor --strict --json
uv run --project .ai/runtime ai obs metrics --json
uv run --project .ai/runtime ai obs health-summary --json
uv run --project .ai/runtime ai obs slo --json
uv run --project .ai/runtime ai queue status --json
uv run --project .ai/runtime ai worker status --json
uv run --project .ai/runtime ai report status --json
```

Treat a strict doctor failure as a release blocker. Metrics, health summary, and SLO output are read-only and allowed in CI.
The `audit_chain` doctor check verifies chained audit entries with `prev_sha`; legacy audit prefixes remain readable, while tampering after the chain starts is reported as `prev_sha_mismatch`.
`queue status` and `obs metrics` include `oldest_pending_age_seconds`, `oldest_processing_age_seconds`, and matching job ids so operators can spot backlog drift before leases expire.
`obs health-summary` rolls up doctor failures, singleton worker lock state, queue age, and the latest `dist/release-gate.summary.json` artifact booleans; it exits `0` for status reporting even when `"ok": false`.

## CI Policy

CI is read-only. Write commands are rejected before worker contact unless the command is explicitly dry-run safe.

```bash
CI=true uv run --project .ai/runtime ai obs metrics --json
CI=true uv run --project .ai/runtime ai obs health-summary --json
CI=true uv run --project .ai/runtime ai diagnostics bundle --dry-run --json
CI=true uv run --project .ai/runtime ai render
```

The first two commands should pass. The final command must fail with exit code `16`.
Write commands such as render, queue mutation, worker stop, trust mutation, inbox mutation, notify enqueue, memory append, audit append, diagnostics write, migration, upgrade apply, and index rebuild are denied in CI with exit `16` and a `CI_READ_ONLY` JSON error when JSON output is requested. Read-only commands such as `queue status`, `worker status`, `trust list`, `secrets status`, `inbox list`, reports, metrics, and `worker health` remain allowed; `worker health` does not create a worker token when CI/GitHub Actions is set.

## Worker Lock Recovery

Inspect the singleton worker lock before starting or replacing a worker:

```bash
uv run --project .ai/runtime ai worker status --json
```

Clear stale or corrupt local locks:

```bash
uv run --project .ai/runtime ai worker stop --json
```

If the lock is live on this host, stop the process first. Use `worker stop --force --json` only after confirming the PID is gone or intentionally replacing the local worker:

```bash
uv run --project .ai/runtime ai worker stop --force --reason operator-confirmed --json
```

Cross-host locks are refused even with `--force`; clear them on the host that owns the lock. CI remains read-only: `worker stop --force` is rejected with `CI_READ_ONLY` and exit code `16`.

## Queue Operations

Inspect queue state:

```bash
uv run --project .ai/runtime ai queue status --json
```

Investigate old queue work before release. Strict doctor fails `queue_age` when the oldest pending job is older than 86400 seconds or the oldest processing job is older than 600 seconds. `age_stats_skipped` reports malformed job files ignored by read-only age metrics.

Recover expired leases:

```bash
uv run --project .ai/runtime ai queue recover-expired --json
```

Archive old dead-letter jobs:

```bash
uv run --project .ai/runtime ai queue archive-dead --older-than-days 30 --json
```

Inspect dead-letter jobs before archive or replay planning:

```bash
uv run --project .ai/runtime ai queue dead --json --limit 50
uv run --project .ai/runtime ai queue dead --json --since 2026-01-01T00:00:00Z
```

Dead-letter inspection is read-only and allowed in CI. It omits job payloads, returns newest failures first, caps `--limit` at 500, and reports malformed dead-letter files as `skipped`.

The queue uses P0-P3 priorities and stores jobs under `.ai/memory/queue/`. Dead-letter files stay local until archived.

## Trust And Secrets

Initialize a trusted machine:

```bash
uv run --project .ai/runtime ai trust init --name "$(hostname -s)" --json
uv run --project .ai/runtime ai render --json
uv run --project .ai/runtime ai doctor --strict --json
```

The private identity is ignored under `.ai/cache/identity/`. The tracked public trust record lives under `.ai/trust/machines/`. Re-render after trust changes so `.ai/generated/manifest.json` reflects the new trust hash.

Check secret status without exposing values:

```bash
uv run --project .ai/runtime ai secrets status --json
```

Do not commit plaintext secrets. The doctor secret scan treats tracked source secrets as a blocker.

## Diagnostics

Generate a dry-run bundle preview:

```bash
uv run --project .ai/runtime ai diagnostics bundle --dry-run --json
```

Generate a local bundle for incident handoff:

```bash
uv run --project .ai/runtime ai diagnostics bundle --json
```

Prune old bundles:

```bash
uv run --project .ai/runtime ai diagnostics prune --keep-days 30 --json
```

Diagnostics payloads are redacted and written under `.ai/cache/diagnostics/`. Share the generated zip only after checking that the receiving party is authorized for repository metadata.

## Upgrade And Rollback

Plan before applying:

```bash
uv run --project .ai/runtime ai upgrade plan --target-version 0.1.1 --json
uv run --project .ai/runtime ai upgrade apply --target-version 0.1.1 --dry-run --json
```

Run the rollback drill before release handoff:

```bash
make rollback-drill
```

The drill copies the repository to a temporary directory, verifies `upgrade apply --dry-run` does not create a backup, creates a rollback backup in the copy, simulates manifest drift, restores through `upgrade rollback`, and runs strict doctor in the copy. It must leave the original worktree clean.

Apply only after the plan is compatible:

```bash
uv run --project .ai/runtime ai upgrade apply --target-version 0.1.1 --json
uv run --project .ai/runtime ai doctor --strict --json
```

Rollback uses the backup path returned by `upgrade apply`:

```bash
uv run --project .ai/runtime ai upgrade rollback --backup-path .ai/cache/upgrade/rollback-<stamp>.json --json
uv run --project .ai/runtime ai doctor --strict --json
```

Clean local rollback cache only after the release is stable:

```bash
uv run --project .ai/runtime ai upgrade clean-cache --json
```

Clean ignored runtime and release artifacts:

```bash
make clean-cache
make clean-artifacts
make clean-all
```

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| `doctor` reports manifest drift | Generated manifest is stale after config or trust changes | Run `uv run --project .ai/runtime ai render --json`, then rerun doctor |
| `doctor` reports secret scan failure | A tracked file contains a token-like value | Remove the secret, rotate it outside this repo, rerun doctor |
| `doctor` reports trust failure | Public machine record is malformed or has an invalid status | Fix or recreate the file with `ai trust init`, then render |
| `doctor` reports `audit_chain` failure | A chained audit JSONL line or its predecessor changed after append | Preserve the file for investigation, compare against release artifacts or backups, then restore trusted audit history |
| SQLite FTS5 or JSON1 check fails | Python SQLite was built without required extensions | Use the bundled `uv` Python environment or rebuild Python with FTS5 and JSON1 |
| Queue has stuck processing jobs | Worker lease expired or worker exited mid-job | Run `ai queue recover-expired --json`, then inspect `ai queue status --json` |
| Dead-letter count grows | Jobs are failing repeatedly | Inspect dead-letter JSON locally, fix the producer or worker, then archive old dead jobs |
| CI write command fails with exit `16` | Read-only CI policy is working | Use dry-run commands in CI or run write commands locally |
| Install check cannot find archive | Package was not built first | Run `./scripts/package.sh`, then `./scripts/install-check.sh` |
| Release gate leaves ignored artifacts | Expected cache, venv, package, or pytest output | Verify `git status --short` is empty; ignored artifacts are acceptable |

## Handoff Checklist

Before handing the repository to another operator:

```bash
./scripts/docs-check.sh
./scripts/release-gate.sh
make env-check
make lockfile-check
make lock-check
make lint
./scripts/verify-artifacts.sh dist/code-brain-0.1.0.tar.gz
./scripts/artifact-tamper-check.sh
make release-gate
uv run --project .ai/runtime ai report status --json
uv run --project .ai/runtime ai report release-gate-summary --json
uv run --project .ai/runtime ai worker status --json
git status --short
```

Attach `dist/release-gate.summary.json`, `dist/code-brain-<version>.release-notes.md`, the archive checksum from `dist/code-brain-<version>.tar.gz.sha256`, `dist/code-brain-<version>.manifest.json`, `dist/code-brain-<version>.sbom.json`, and `dist/code-brain-<version>.provenance.json`.
Attach `dist/dep-advisory.json` as an advisory dependency vulnerability report. Findings or offline skips do not fail the release gate unless a future hard-fail policy is explicitly approved.
