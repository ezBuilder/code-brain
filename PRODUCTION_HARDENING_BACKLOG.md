# Production Hardening Backlog

This backlog tracks remaining commercial-readiness work after the current release gate. It is intentionally operational and implementation-focused; it does not replace the Claude-authored PRD.

## Release Safety

- Verify every release from a clean tracked tree with `./scripts/release-gate.sh`.
- Keep `release_ready`, `release_artifacts.all_valid`, and `release_artifacts.all_current` as hard blockers.
- Keep `dist/release-gate.summary.json` attached to every CI run and handoff bundle.
- Add release tag signing only after key ownership and rotation are documented.
- Keep rollback drills in the release gate so upgrade backup and rollback paths are checked before handoff.
- Add a changelog generator only if it consumes existing release notes and does not rewrite PRD content.

## CI And Read-Only Policy

- Keep GitHub Actions permissions at `contents: read`.
- Keep checkout `persist-credentials: false`.
- Keep CI write probes for representative mutation commands: render, queue mutation, worker stop, trust revoke, inbox decision, diagnostics prune, migration, upgrade rollback, and index rebuild.
- Add a workflow status observation step after pushed `main` runs are available.
- Add branch protection outside this repository: require release gate, forbid direct force pushes, and require signed commits if the operator policy demands it.
- Do not introduce CI secrets until an explicit secret inventory and redaction test exists.

## Worker And Queue Operations

- Keep singleton worker lock cleanup explicit through `ai worker stop`.
- Do not auto-clear cross-host worker locks.
- Keep `queue_lock` around every queue mutation.
- Add a worker-loop integration only when there is an actual daemon lifecycle, systemd unit, or supervisor contract.
- Keep queue age metrics and doctor thresholds visible for oldest pending and oldest processing jobs.
- Keep dead-letter inspection read-only and payload-free before adding any dead-letter replay command.
- Keep dead-letter replay gated by an explicit operator action.

## Observability And Diagnostics

- Keep diagnostics redacted and zip contents whitelisted.
- Add log volume and retention checks once real operator logs exist.
- Add `ai obs health-summary --json` only if it summarizes existing doctor, queue, worker, and artifact state without new writes.
- Keep absolute local paths redacted in external-facing diagnostics and CI summaries.
- Add runbook examples for common doctor failures as they appear in real operation.

## Artifact And Supply Chain

- Keep package verification before install verification.
- Keep artifact tamper checks covering checksum, manifest, SBOM, provenance, and release notes.
- Add archive extraction path traversal tests if package contents expand beyond the current controlled file list.
- Add dependency vulnerability scanning only as a read-only advisory gate unless the operator approves a hard fail policy.
- Keep SBOM lockfile hash validation.

## Fresh Clone And VPS Handoff

- VPS live testing is deferred to the operator.
- Preserve `scripts/preflight.sh --check-only --json` as the first failure point for fresh clones.
- Keep bootstrap failures clear when `uv`, Python, optional PowerShell, `sops`, `age`, or Git LFS are missing.
- Add VPS-specific notes only after the operator reports the actual target OS, shell, and deployment path.
- Keep local cache and release artifacts ignored, never tracked.

## Security And Secrets

- Keep plaintext secrets out of tracked files.
- Keep encrypted secret tooling conditional until encrypted files exist.
- Keep `--no-redact` unavailable for external outputs.
- Add secret fixture coverage when new token formats are encountered.
- Do not add network calls to hooks, MCP hot paths, doctor hot path, or CI probes.

## Documentation Completeness

- Keep README focused on quick start and implemented command surface.
- Keep OPERATIONS.md as the operator runbook.
- Keep RELEASE.md as the release checklist.
- Keep this backlog as the active commercial hardening queue.
- Update the checkpoint after every completed hardening round with commit, verification commands, artifact hashes, and next candidates.
