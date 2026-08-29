# Operations Runbook

This runbook is for operating Code Brain after handoff. It assumes a repo-local install where `.ai/` is the only source of agent runtime state.

## First Run

```bash
cd code-brain
make env-check
make preflight
make lockfile-check
make lock-check
make session-start
make lint
make quick
./bootstrap.sh
uv run --project .ai/runtime ai doctor --strict --json
uv run --project .ai/runtime ai report status --json
uv run --project .ai/runtime ai report release-gate-summary --json
uv run --project .ai/runtime ai worker status --json
uv run --project .ai/runtime ai session start --agent codex --json
```

Expected result:

- `doctor.ok` is `true`.
- `release_artifacts.all_present` and `release_artifacts.all_valid` are `true` after packaging.
- `release_ready` is `true` only when doctor is green, release artifacts are present/valid, and provenance matches the current clean git HEAD.
- The release gate fails when `release_ready` or `release_artifacts.all_current` is not `true`.
- `report.status.ok` is `true`.
- `git.status_short` is empty for tracked files.
- Runtime artifacts appear only in ignored paths such as `.ai/cache/`, `.ai/runtime/.venv/`, `.ai/runtime/.pytest_cache/`, `__pycache__/`, and `dist/`.

Mac/VPS handoff uses GitHub tracked files as the baseline. After `git pull` or fresh clone, run `ai session start` or `make session-start`; local cache, virtualenv, and search index are regenerated on that machine rather than shared through Git.

## Existing Project Install

Attach Code Brain to an existing git project with one command:

```bash
cd code-brain
./scripts/install-into.sh install /path/to/project
./scripts/install-into.sh upgrade /path/to/project
./scripts/install-into.sh uninstall /path/to/project
make install-into TARGET=/path/to/project
make upgrade-in TARGET=/path/to/project
make uninstall-from TARGET=/path/to/project
```

Windows uses the same commands through PowerShell (Git for Windows is required and supplies the delegated Bash runtime):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-into.ps1 install C:\path\to\project
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-into.ps1 upgrade C:\path\to\project
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-into.ps1 uninstall C:\path\to\project
```

The installer records managed files in `.ai/generated/install-manifest.json`, preflights ownership and path confinement, refuses unrelated existing files and symlink escapes, and skips byte-identical protected files. Install, upgrade, and uninstall use a write-ahead journal at `.code-brain-install-transaction`: every managed/config path, prior `core.hooksPath`, and runtime-venv intent is durable before mutation; file backups carry SHA-256/size receipts. ERR/INT/TERM rolls back immediately, while a later invocation recovers an interrupted READY journal after SIGKILL/power loss before parsing or changing target config. COMMITTED is fsynced before old backups are removed. A corrupt/untrusted journal or backup fails closed and remains for forensics. Uninstall removes managed wires but preserves `.ai/memory/`, `.ai/runtime/state/`, `.ai/eval/`, and the user-owned secret allowlist. Windows delegates file/config mutation to this same transaction, then activates the native PowerShell shims before commit. It rebuilds the audit index, installs git pull/checkout hooks, and runs one forced session rebuild. Local `.ai/cache/` and `.ai/runtime/.venv/` artifacts are regenerated per machine.
For pre-manifest partial installations, use `upgrade`: adoption is enabled only when the runtime package name, Codex Code Brain hook command, and Code Brain MCP table are all present. A lone `.ai` or similarly named runtime remains collision-gated, while authoritative `.ai/memory/` data is preserved during a verified migration. Source-side `.ai/outputs/` artifacts are never part of the managed payload (except its scaffold `.gitkeep`); upgrading an older install transactionally removes only untracked output paths that its previous manifest marked as Code Brain-managed and leaves all target-created or Git-tracked outputs intact.

The default install keeps the runtime dependency-light and uses BM25 search. Provision the optional ONNX dense-search dependencies only when required:

```bash
AI_INSTALL_DENSE=1 ./scripts/install-into.sh install /path/to/project
AI_INSTALL_DENSE=1 ./scripts/install-into.sh upgrade /path/to/project
```

Dense search remains separately controlled at runtime by `AI_SEARCH_DENSE`; missing optional dependencies degrade safely to BM25.

For package images, CI fixtures, or other staged provisioning, defer the expensive local runtime activation while still writing every managed file and the install manifest:

```bash
AI_INSTALL_DEFER_RUNTIME=1 ./scripts/install-into.sh install /path/to/project
cd /path/to/project
./bootstrap-code-brain.sh --skip-doctor --skip-render
.ai/bin/ai session start --agent operator --rebuild auto --repair-audit-index --render-manifest --json
```

The defer flag is explicit and fail-safe: normal installs still bootstrap, repair the audit index, and create the first session before returning.

The one-command `scripts/install.sh` wrapper requests strict health on that same first session. It does not launch a second doctor process or create another session snapshot.

Run normal startup in the target project:

```bash
cd /path/to/project
.ai/bin/ai session start --agent codex --query "current task" --json
.ai/bin/ai doctor --strict --json
```

`session start` can return `ok: true` while `doctor.ok` is `false` when strict quality warnings exist, such as tracked plaintext secret candidates. Treat `doctor --strict` as the release gate, not as a blocker for initial attachment.

Normal `session start` is the low-latency attachment path. It may reuse root-confined, private local state for unchanged files and reports the security-scan work explicitly as `mode=incremental baseline=cache total=... reused=... rescanned=... unreadable=... unstable=...`. Reuse is bound to file device, inode, mode, size, nanosecond mtime, nanosecond ctime, symlink payload, and the current secret-matcher fingerprint. A replaced, unstable, unreadable, publicly writable, foreign-owned, symlinked, or out-of-root cache entry is never trusted; affected files are rescanned or conservatively surfaced.

`doctor --strict` remains authoritative. It bypasses tracked-file and search-candidate caches, reads the live Git baseline, re-hashes indexable source content, performs the full tracked-file credential scan, and runs the full diagnostics and synthetic hot-path checks. A project without `.git` uses a pruned `baseline=filesystem` that excludes Code Brain state, virtual environments, dependency trees, build output, and `.chatgpt2codex`; a project with a `.git` marker but an unreadable Git baseline fails explicitly instead of silently scanning untracked local state. Local caches improve interactive startup only; they cannot weaken the release or security gate.

## Search Cache Profile

`.ai/cache/code.sqlite` uses SQLite FTS5 for lexical code search. The cache stores file paths, hashes, summaries, provenance, and a contentless FTS index; it does not store duplicate full source bodies in the `chunks` table. Query snippets are read lazily from the current source file and redacted before output. Sources above 100KB are streamed into overlapping bounded windows plus capped function/class spans; Python, Rust, TypeScript, JavaScript, Dart, and Kotlin use the same path. Results are grouped by real source before snippet hydration so windows cannot crowd other files out or trigger repeated reads. Files above the 32MB defense cap and every generated/binary/data/lock/encoding/unsupported/trust omission appear in index diagnostics with a class and reason; generated bundles receive searchable path-only classification stubs.
If a source file changes after indexing, local query paths auto-refresh before searching: current tracked dirty paths from `git status` reuse per-file size/mtime_ns/ctime_ns state and hash only on metadata drift, while deletions, rename sources, newly untracked files, and clean-tree checkout/pull drift are reconciled by the authoritative Git-candidate comparison. A staged deletion with an ignored working-tree copy is never re-indexed, preventing an add/remove generation loop. The interactive path can reuse a private candidate-list cache. Strict freshness checks and full rebuilds bypass that cache and query Git directly. Internal runtime state under `.ai/memory/`, `.ai/cache/`, and `.chatgpt2codex/` is excluded from the code index. CI remains read-only; set `AI_SEARCH_AUTO_REFRESH=0` to force stale-report-only behavior.

### Stale Index Handling

`ai obs search --query <text>` refreshes stale local indexes automatically before retrieval. The JSON payload includes `query.auto_refresh.reason` (`dirty_paths`, `mtime_fallback`, `missing`, or `current`) so operators can see whether the query touched only changed paths or had to scan more broadly. If auto-refresh is disabled or blocked by CI and any returned hit references a source whose sha256 has drifted from the indexed value, the command exits `13` (`MANIFEST_DRIFT`) with a `query.remediation` block.

- **Default local path**:
  ```bash
  .ai/bin/ai obs search --query "<text>" --json
  ```
- **Manual full refresh**:
  ```bash
  .ai/bin/ai index rebuild --json
  .ai/bin/ai obs search --query "<text>" --json
  ```
- **Force full refresh before query** (rejected in CI):
  ```bash
  .ai/bin/ai obs search --refresh-stale --query "<text>" --json
  ```
  `--refresh-stale` triggers a full `rebuild` before the query and exits `0`. It is a write operation and the CI policy gate rejects it unless the runner permits writes.

`ai session start --rebuild auto` (default during `make session-start`) performs the same refresh implicitly when the index is stale at session boundary.

The same session boundary enforces workspace storage retention: `.ai/tmp` is limited to 512MB/256 top-level entries with seven-day retention, `.ai/outputs` to 1GB/512 entries, derived `.ai/memory/episodic` to 128MB/eight entries, derived `.ai/memory/audit-rollups` to 64MB/16 entries, and other reclaimable `.ai` data to 2GB. Oldest untracked scratch entries are reclaimed before outputs. Git-tracked, `.keep`-pinned, authoritative audit/decision/session memory, and `.ai/memory/episodic-tombstones` bytes are reported but excluded from automatic reclaim limits; Code Brain never deletes source-of-truth or forget state to make a cap pass.

Raw memory remains ignored and local-private during install and upgrade. Do not track only the current audit file. If cross-machine memory is required, first verify that the configured Git remote is private, then run:

```bash
.ai/bin/ai memory sync --private-remote-confirmed --json
```

That explicit mode stages the complete authoritative memory snapshot, including every ignored raw audit segment and current file, under the audit transaction lock. Locks, queues, events, audit indexes, rollups, and episodic tiers are excluded. Audit files have union merge disabled; a rebase conflict or missing segment aborts sync instead of concatenating or re-chaining evidence. `--no-push` keeps the resulting memory commit local. Never use the confirmation flag with a public remote.

Large project cache checks:

```bash
du -sh .ai .ai/cache .ai/runtime/.venv
sqlite3 .ai/cache/code.sqlite "pragma user_version; select count(*) from chunks;"
.ai/bin/ai index rebuild --json
```

Vector RAG is intentionally not enabled by default. SQLite can support vector search through extensions such as `sqlite-vec` or `sqlite-vss`, but those are extra runtime dependencies and local embedding models add disk and install weight. Keep vector search opt-in, local-only, and fallback-safe unless a target project proves FTS5 recall is not enough.
The default `.ai/config.yaml` sets `search.retriever: bm25`. Setting `vector` or `hybrid` is accepted as an explicit future intent but fails doctor/query until the optional vector stack is installed and implemented.

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
Bootstrap records that successful result in `.ai/cache/preflight-proof.json`. The proof is mode `0600`, root-confined, and bound to the preflight script hash, repository root, PATH/tool binary states, runtime Python, encrypted-secret requirements, Git attributes, bootstrap files, cache permissions, and relevant environment variables. Doctor may reuse an unchanged proof for up to one hour; any fingerprint, ownership, permission, symlink, path-confinement, or age mismatch forces the real preflight command to run again.
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

`scripts/package.sh` archives exactly `git ls-files`, rejects tracked symlinks, and normalizes path order, ownership, mode, and timestamps. Ignored runtime memory, `.ai/tmp`, local `.claude/settings.json`, and generated root instruction mirrors can neither leak into an archive nor make its size grow with ordinary use.

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
uv run --project .ai/runtime ai obs usage --json
uv run --project .ai/runtime ai obs search --query "current task" --json
uv run --project .ai/runtime ai obs health-summary --json
uv run --project .ai/runtime ai obs slo --json
uv run --project .ai/runtime ai queue status --json
uv run --project .ai/runtime ai worker status --json
uv run --project .ai/runtime ai report status --json
```

Treat a strict doctor failure as a release blocker. Metrics, usage, search, health summary, and SLO output are read-only and allowed in CI.
`obs usage` reports actual token fields only when they come from agent session transcripts. Claude Desktop/Claude Code usage is read from `CLAUDE_HOME` or `~/.claude/projects/*/*.jsonl` and aggregated from `message.usage`. Codex usage is explicitly reported as `codex_not_implemented` until its local session log format is treated as stable. Code Brain does not convert bytes to tokens or claim token savings.
`obs search --query ...` shows cache size, indexed file bytes, returned context bytes, stale results, and retriever mode so operators can visually verify whether Code Brain is returning small, fresh context packs instead of broad source dumps.
The `audit_chain` doctor check verifies each file's `prev_sha` chain plus immutable-segment filename digests and cross-file marker path/whole-file hash/last-line hash/byte-count links. Duplicate `(year, sequence)` branches, duplicate event IDs, malformed modern rows, and any mismatch are hard failures. Legacy ID-less prefixes remain readable but are explicitly reported as unverifiable; `_folded` rows remain visible as irreversible historical loss.
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

### FTS5 Tokenizer (schema_version=3)

`.ai/cache/code.sqlite` `chunks_fts` is created with `tokenize="porter unicode61 remove_diacritics 2"` so that searches match inflected forms (`run` ↔ `running`/`runs`) and accent-stripped variants (`café` ↔ `cafe`). Legacy v2 caches are auto-detected by `init_schema` and rebuilt on first call. Operator action required only if `ai obs search` returns no hits where prior versions did — run `ai index rebuild` once.

### Shell Sandbox (token-cost guard)

For shell commands with potentially large output (`grep`, `find`, `cat`, `tree`, `curl` dumps), Code Brain provides a sandbox that stores full output to disk and returns only a short summary to the model:

```bash
ai exec run --timeout 30 -- grep -rn "useEffect" src/        # short summary
ai exec run --isolate-network --isolate-env -- python3 check.py # deny network + inherited secrets
ai exec fetch --exec-id <id> --line-start 100 --line-end 200 # specific range
ai exec fetch --exec-id <id> --grep "useEffect.*deps"        # filter by substring
ai exec list --json                                          # recent executions
ai exec prune --older-than-seconds 86400                     # clean cache (24h default)
```

Storage: `.ai/cache/sandbox/<exec_id>.{txt,meta.json}` (mode `0o600`, gitignored). MCP equivalents: `sandbox_execute`, `sandbox_fetch`, `sandbox_list`. Execution `cwd` must resolve inside the repository; output IDs are fixed 16-character lowercase hex values, symlink artifacts are rejected, and fetch/read paths are capped at 4 MB. The summary (first 30 lines + last 5 lines, capped 4 KB) replaces a 50–500 KB raw grep dump in the model's context window. Network and environment isolation are opt-in for compatibility: use both `--isolate-network --isolate-env` (or matching MCP booleans) for untrusted metric commands; `--extra-env NAME` may add reviewed non-secret variables, while secret-looking names fail closed. `sandbox_execute` is in `WRITE_COMMANDS` and rejected in CI unless explicitly run with the same write policy as `index rebuild`.

### Cross-Session Memory (proactive logging)

Code Brain's central feature is *cross-session context sharing*. Claude receives last-N decisions/todos/session notes/resume state through `SessionStart`; Antigravity auto-loads the managed root `AGENTS.md`; Codex uses that same file when its bounded durable-state fingerprint is current and receives only live deltas through the hook. If the file is stale or untrusted, Codex safely falls back to full hook context. Operators can log directly:

| Operation | CLI | MCP tool |
|---|---|---|
| Record decision | `ai memory decision add --text "..." [--tag x]` | `record_decision({text, tags})` |
| Record todo | `ai memory todo add --title "..." [--owner x]` | `record_todo({title, owner, tags})` |
| Close todo | `ai memory todo close --match "<id|substring>"` | `close_todo({match, status, reason})` |
| Append session note | `ai memory session append --text "..."` | `append_session_note({text})` |

All four are write-class (rejected in CI per `WRITE_COMMANDS`). Records auto-redact via `redact_value` before persisting. Decisions are append-only; closing a todo writes a new line with `status="done"` and preserves the original open line for audit.

Judge memory by useful, current decisions/todos/resume state being recalled with bounded context bytes—not by SessionStart bytes increasing. Stable or falling bytes are expected when Codex deduplicates an already current `AGENTS.md`; growth without better recall is a regression signal.

Prompt growth and memory-ranking extras are not required for normal memory correctness. Enable them only after measuring value with `AI_PROMPT_GROWTH=1`, `AI_MEMORY_PAGE_IN=1`, `AI_MEMORY_TIER_SUMMARY=1`, `AI_AUTO_PAGE_OUT=1`, or `AI_CODEGRAPH_SUMMARY=1`; the transcript-token refresher is completely dormant while prompt growth is off. Detached sleep-time page-out always performs offline, non-destructive audit-rollup and episodic-index maintenance even when those extras are off.

### Episodic audit memory

```bash
.ai/bin/ai memory episodic build --dry-run --json
.ai/bin/ai memory episodic build --json
.ai/bin/ai memory episodic status --json
.ai/bin/ai memory episodic context --byte-budget 8000 --raw-tail 20 --json
.ai/bin/ai memory episodic drill-down --event-id <evt-id> --json
.ai/bin/ai memory episodic drill-down --start 100 --end 120 --limit 50 --json
```

Treat context summaries as a non-authoritative index. Check `receipt.uncovered` and `source_truth_complete`, then drill down before an important judgment. An unchanged build must report a no-op with no tier/meta/cache growth. `status.integrity_ok=false`, a segment-link error, or a duplicate sequence is a release blocker; restore trusted raw bytes or run the explicit `ai audit repair-chain` workflow for a known merge splice. Repair never chooses between divergent same-sequence branches.

The raw audit grows linearly and is retained deliberately. Derived tier rows and injected context grow logarithmically. Explicit build/status/context/drill-down validate the raw corpus in linear time, while `SessionStart` only checks source metadata and reads the prebuilt 200-byte `cb-life:` cache. Full design and failure semantics: [Episodic Memory and Lossless Audit History](docs/EPISODIC_MEMORY.md).

Cross-machine sync is explicit because hooks and MCP never cause network traffic, even through detached children. Run `.ai/bin/ai memory sync` for one cycle or supervise `.ai/bin/ai memory sync --loop` outside the hook lifecycle. A legacy `memory_sync.enabled: true` setting is a diagnosed no-op and should be removed.

### Session Resume Snapshots

`ai session start` writes `<.ai/memory/sessions/<sid>/resume.json>` (mode `0o600`, schema_version 1, capped 4 KB). Each snapshot contains last 5 decisions, last 5 open todos, last 12 lines of `session-current.md`, and last 10 distinct audit actions — all redacted. The `SessionStart` hook auto-injects the *prior* session's snapshot (excluding the current `session_id`) into `additionalContext`, so a fresh Claude/Codex session inherits tail state after compaction or `--resume`. Pruned automatically after 14 days (`session_resume.prune_snapshots`).

### Secret Scan Allowlist

When Code Brain operates inside a target project (host = the project repo), `ai doctor`'s `secret_scan` check inspects every git-tracked file. Some legitimate target-repo files match heuristic patterns (e.g. `firebase_options.dart` generated config, fixture JSONs, internal source maps). The doctor report distinguishes two states:

- **Flagged**: pattern hit, no acknowledgment — `secret_scan` fails (`ok=false`).
- **Acknowledged**: pattern hit, path listed in `.ai/secret_scan_allowlist.txt` — `secret_scan` reports the count in the detail line and remains `ok=true`.

Maintain `.ai/secret_scan_allowlist.txt` (one repo-relative path per line; `#` comments allowed). Hardcoded ignores already cover well-known noise: lockfiles (`package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `bun.lock`, `Cargo.lock`, `Gemfile.lock`, `composer.lock`, `go.sum`, `poetry.lock`), generated assets (`firebase_options.dart`, `*.map`, `*.min.js`, `*.min.css`), and tool output trees (`.playwright-mcp/`, `.dart_tool/`, `source-maps/`).

The allowlist is for *reviewed-and-acknowledged false positives only*. Real secret-bearing material must use SOPS+age-encrypted `.ai/secrets/*.enc.yaml`; never list a real secret path in the allowlist.

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
| `doctor` reports `no_token_estimates` failure | A guarded source file (`obs.py` / `report.py` / `session.py` / `transcripts.py` / `search.py`) introduced a forbidden estimate-style identifier | Replace any `estimate_tokens` / `tokens_saved` / `estimated_tokens` / `token_savings` symbol with measured-only fields and rerun doctor |
| `ai obs search` exits `13` (`MANIFEST_DRIFT`) | One or more results reference source whose sha256 drifted since last index rebuild | Run `.ai/bin/ai index rebuild --json`, or rerun the search with `--refresh-stale` for an explicit auto-refresh (writes the cache; CI rejects this) |

## Slash Commands and MCP

Code Brain ships seven slash commands per agent client plus a project-local MCP server registration. The retrieval proof backend performs bounded local A/B calls and may refresh a stale index during warm-up; its measured repetition phase must not mutate retrieval files.

### Claude Code (`/cb-*`)

Project-level slash commands live under `.claude/commands/cb-*.md`:

| Command | Backend | Read-only |
|---|---|---|
| `/cb-usage` | `ai obs usage --json` (actual transcript tokens; no estimates) | yes |
| `/cb-health` | `ai obs health-summary --json` | yes |
| `/cb-search [query]` | `ai obs search --query "$ARGUMENTS" --json` (exit `13` on stale) | yes |
| `/cb-doctor` | `ai doctor --strict --json` | yes |
| `/cb-proof [query]` | `ai context prove [query] --json` | after warm-up |

Command markdown forbids fabricated measurements and requires direct backend output. The Code Brain doctor `mcp_methods_registered` check fails if any required command is missing.

### Codex CLI (`.codex/prompts/cb-*.md`)

The same seven operations are mirrored under `.codex/prompts/cb-*.md`. Codex CLI command-registration conventions vary by version — verify your version's prompt-discovery path matches `.codex/prompts/` and adjust `.codex/config.toml` if needed.

### MCP server (both clients)

`.mcp.json` registers the Code Brain stdio MCP server (`.ai/bin/ai-mcp`) at the project level. Claude Code auto-loads it. Codex picks it up via `.codex/config.toml`'s `mcp_servers.code-brain` block.

Available MCP methods (read-only except `ai_request_rebuild`):

- `obs_usage`, `obs_health_summary`, `obs_search`, `doctor_strict`
- `memory_query`, `code_query`, `context_pack`, `ai_status`
- `ai_request_rebuild` (the only write method; enqueues a rebuild job)

The doctor check `mcp_methods_registered` enforces that:
1. `.mcp.json` registers `mcpServers.code-brain` with `command = ".ai/bin/ai-mcp"`.
2. All four Claude slash command files exist.
3. All four Codex prompt files exist.

### PreToolUse Auto-Routing (Claude/Codex/Kiro)

`.claude/settings.json` (Claude Code), `.codex/hooks.json` (Codex CLI), and `.kiro/hooks/code-brain.json` (Kiro IDE/CLI v3) register `PreToolUse` hooks before tools execute — this is Code Brain's "auto-routing" of long-output shell commands and its file-write stream guard. `precall.evaluate` decides whether a shell command would dump large output into the model's context window. Kiro CLI v2 does not load the standalone v1 hook file; it remains an inert forward-compatible seed until the host migrates to v3.

Intercepted patterns (denied with redirect message):
- `grep -r` / `grep -R` / `grep -rn` (recursive grep)
- `rg <anything>` (ripgrep — recursive by default)
- `find <anything>`
- `tree <anything>`
- `ack`, `ag`

Allowed (passes through to Bash):
- `grep pattern file.txt` (single-file)
- Anything piped to `| head`, `| tail`, `| wc`, `| less`, `| more`
- Anything redirected to `/dev/null` (`2>/dev/null`, `>/dev/null`)
- Compound commands with `&&`, `||`, `;` (conservative — too complex to analyze)

When intercepted, the hook returns `decision: "block"` with a reason instructing the agent to retry via `ai exec run -- <original>` or MCP `sandbox_execute`. The agent normally re-issues the call against Code Brain's sandbox, which stores full output to `.ai/cache/sandbox/<exec_id>.txt` and returns only a short summary (first 30 + last 5 lines, ≤4 KB) to the context window.

Disable: remove the `PreToolUse` block from `.claude/settings.json` (Claude Code), the `PreToolUse` key from `.codex/hooks.json` (Codex CLI), or disable the matching row in `.kiro/hooks/code-brain.json` (Kiro). The `precall` heuristic itself stays loaded but never fires without hook registration.

### Codex Hook Trust After Upgrades

[Codex hook trust](https://learn.chatgpt.com/docs/hooks) is tied to the exact hook-definition hash.
A newly installed or changed hook is therefore skipped until that hash is trusted; the CLI's
`--dangerously-bypass-hook-trust` switch applies only to that invocation and is not a persistent
desktop setting.

After a normal non-CI install or upgrade, Code Brain now bootstraps Codex project trust and persists
the current hashes of the exact managed project hooks by default. This happens only after the target
transaction commits. The no-policy path does not create or mutate a policy file, does not trust
global user hooks, and requires all of the following before a first project-trust write:

- `.ai/bin/ai-hook` and `.ai/bin/ai-hook.ps1` are safe regular files and match the installer source
  byte-for-byte; every Code Brain-owned `.codex/hooks.json` group matches the shared canonical
  contract, including target-specific `SessionEnd`/`Interrupt` version gates;
- the target's parsed `.codex/config.toml` equals the managed Code Brain config, so project trust
  cannot silently activate a custom MCP command;
- Codex's live app-server accepts and then reports both the project-trust and exact hook-hash writes.

Foreign hook groups are preserved but excluded from the automatic hash-trust write; modified Code
Brain groups, changed routers, unsafe files, and custom project configs remain review-gated. An
unavailable/rejecting Codex app-server also leaves the hooks review-gated without failing the managed
installation. Set `AI_CODEX_HOOK_AUTO_TRUST=0` to opt out. CI,
`GITHUB_ACTIONS`, `GITLAB_CI`, and `AI_CI` default to opt-out; set
`AI_CODEX_HOOK_AUTO_TRUST=1` explicitly to enable the same verified path there.
Before a normal uninstall removes the manifest entries, it deletes only the live app-server keys for
exact Code Brain project-hook commands. Existing project trust, foreign project-hook hashes, and
global user-hook hashes are retained; an unavailable app-server cannot block filesystem cleanup.

An explicit private policy remains available for reviewed custom project configs or exact global
user-hook scripts:

```json
{
  "schema": 1,
  "trust_project_code_brain_hooks": true,
  "trusted_project_roots": ["/absolute/path/to/project"],
  "trusted_user_hook_paths": [
    "/absolute/path/to/.codex/hooks/my-reviewed-hook.sh"
  ]
}
```

Save it as `${XDG_CONFIG_HOME:-$HOME/.config}/code-brain/codex-hook-trust.json` with mode `0600`,
or point `AI_CODEX_HOOK_TRUST_POLICY` at an absolute policy path. The helper persists only current
hashes of exact Code Brain project-hook commands and explicitly allowlisted direct user-hook scripts.
The default-location policy augments the managed default: a newly installed exact target outside its
project roots still gets managed-project trust while retaining its approved user-hook paths. A policy
selected explicitly with `AI_CODEX_HOOK_TRUST_POLICY` remains authoritative and has no such fallback.
Prefer listing each project root explicitly rather than allowlisting an entire workspace. Targets
outside `trusted_project_roots`, foreign commands, content drift, symlinks, wrong-owner files, and
group/world-writable policy or hook files remain untrusted. An existing policy is never generated or
rewritten by the installer.
Missing entries in the default policy are ignored without rewriting it, so deleting an old workspace
or user-hook script cannot disable exact managed-target trust for unrelated upgrades. Dangling
symlinks and all malformed or unsafe existing entries still fail closed. An explicitly selected
`AI_CODEX_HOOK_TRUST_POLICY` remains strict and does not receive this stale-entry fallback.
For a linked Git worktree, Codex loads project hooks from the main worktree; the helper resolves that
root through Git, requires the linked-worktree routers to match, and semantically validates both hook
manifests before trusting only canonical Code Brain entries surfaced from the main worktree.

### Completion Guard And Turn Summary

`AI_COMPLETION_GUARD` defaults to enabled. At `UserPromptSubmit` (or Antigravity's first
`PreInvocation`) Code Brain captures a bounded request baseline. `PostToolUse` records a private,
bounded mutation/check ledger without Git/network: host call identity, successful exit provenance,
and cumulative edit-target content hashes. Stop/SubagentStop is continued when new
machine evidence remains unfinished, including an edit without a later successful relevant
test, lint, build, doctor, docs check, or `git diff --check`. Code edits require the stronger
static/build/test class; a failed command, a test name printed with `echo`, or a check run
before the last edit does not satisfy the gate. Content changed after verification invalidates that
proof. Missing/stale/corrupt baseline or ledger, unbindable tool/path evidence, and a marker scan over
the 40-path bound all yield instead of turning partial evidence into a loop.

Safety is bounded: authenticated user-input/context-pressure/terminal host stops yield,
security decisions take precedence, identical evidence yields after two nudges, and all
continuations share an eight-attempt/30-minute repository/worktree + host-session budget. Cap release
is recorded and Claude receives one user-visible, non-reentrant system warning. For the defined
evidence classes, golden tests require cross-host decision equivalence; detection remains partial to
observable PostToolUse and bounded tree evidence. It is not a semantic oracle for requirements that
were never represented by a plan, edit, marker, conflict, or acceptance record.

Claude also applies the same quality evidence to `TaskCompleted` and `TeammateIdle`; an incomplete
task is rejected with Claude's documented exit-2/stderr contract. Kiro can block
`PreToolUse`/`UserPromptSubmit` through a non-zero exit but its `Stop` trigger is advisory, so Code
Brain records the recommendation without claiming it can force another model turn. Codex
`SessionEnd` is installed only on CLI 0.117+, and `Interrupt` only on 0.150+; downgrades prune only
Code Brain's own gated entries while preserving foreign hooks.

Every managed command hook has an explicit timeout: hot-path/blocking events are capped at five
seconds, observation-only events at two seconds, and Codex `SessionEnd` at two seconds under its
three-second host cap. Codex context-producing hooks set `additionalContextLimit` only where the
host accepts it: 5000 tokens for `SessionStart`/`SubagentStart`, 2500 for
`UserPromptSubmit`. `doctor --strict` validates these values, file-write matcher coverage, version
gates, and each host's active/disabled surface.

Compatibility stays fail-closed. Claude Code 2.1.251's changelog mentions
`PreModelSwitch`/`PostModelSwitch`, but Code Brain does not emit them until their payload and
blocking contracts are part of the stable hook reference. Antigravity keeps `PreToolUse` and
`PostInvocation` disabled because its native decision schemas have not yet passed Code Brain's live
allow/continue regression gate; the three proven hooks remain active and `doctor` reports the two
disabled surfaces instead of pretending full coverage.

The [current Claude hook reference](https://code.claude.com/docs/en/hooks) also exposes `Setup`,
`UserPromptExpansion`, `PostToolBatch`, `WorktreeCreate`, `WorktreeRemove`, `Elicitation`, and
`ElicitationResult`. They are intentionally not duplicated in the project hook set: setup is owned
by the transactional installer; per-tool `PostToolUse` already supplies the evidence ledger that a
second batch hook would duplicate; direct command expansion has no separate Code Brain deny policy;
`WorktreeCreate` would replace Claude's default Git worktree implementation; and Code Brain must not
intercept or rewrite user/MCP elicitation answers. `PermissionRequest` is likewise not used to
auto-approve Claude operations—`PreToolUse` enforces project safety before the host permission
dialog. Add one of these surfaces only with a concrete policy, host-version gate, native payload
fixture, and allow/block regression proof; event count alone is not an optimization target.

Broad-change reporting uses the standing Response rule in the generated agent contract.
`turn_report` measures Git facts in a detached child at Stop/SessionEnd and injects one terse
next-turn nudge only above eight files or 200 churned lines. A hard prose-only Stop continuation
is intentionally not used: Claude/Antigravity can re-enter, but that spends the same bounded
budget as correctness/security and adds another model turn.

## Browser Extension Dogfood Runbook (target = WXT/Manifest V3)

Code Brain treats a built browser extension as a verifiable artifact when the host project includes a Manifest V3 build (e.g. WXT outputs to `.output/chrome-mv3/`). No new product feature is shipped for this; the runbook below uses existing read-only commands.

```bash
# Inside the target repo (host = target). Code Brain operates as host.
.ai/bin/ai session start --agent operator --json     # auto-rebuild stale index

# Build the extension via the project's own build (example: WXT + bun)
bun run compile && bun run build                       # produces .output/chrome-mv3/

# Index-aware verification (no Code Brain change to target tree)
.ai/bin/ai index rebuild --json
.ai/bin/ai obs search --query "manifest_version" --json     # confirms manifest in index
.ai/bin/ai doctor --strict --json                            # secret scan honors allowlist

# Manual checks Code Brain does NOT automate (browser-side):
#   1. chrome://extensions → Load unpacked → select .output/chrome-mv3/
#   2. Verify version string in extension popup matches manifest.json version
#   3. Test runtime behavior in target site (per project's QA list)
```

Code Brain does not parse, install, or activate browser extensions. Extension-side runtime checks (popup behavior, content script injection, Naver editor word download, count state, etc.) remain manual or covered by the target project's own test runner.

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
