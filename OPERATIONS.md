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

The installer records managed files in `.ai/generated/install-manifest.json`, refuses to overwrite unrelated existing files, preserves target `.ai/memory/` during upgrades, rebuilds the audit index, installs git pull/checkout hooks, and runs a forced session rebuild once. It does not commit `.ai/cache/` or `.ai/runtime/.venv/`; each Mac/VPS regenerates those local artifacts after `git pull`.

Run normal startup in the target project:

```bash
cd /path/to/project
.ai/bin/ai session start --agent codex --query "current task" --json
.ai/bin/ai doctor --strict --json
```

`session start` can return `ok: true` while `doctor.ok` is `false` when strict quality warnings exist, such as tracked plaintext secret candidates. Treat `doctor --strict` as the release gate, not as a blocker for initial attachment.

## Search Cache Profile

`.ai/cache/code.sqlite` uses SQLite FTS5 for lexical code search. The cache stores file paths, hashes, summaries, provenance, and a contentless FTS index; it does not store duplicate full source bodies in the `chunks` table. Query snippets are read lazily from the current source file and redacted before output.
If a source file changes after indexing, local query paths auto-refresh before searching: dirty/untracked/deleted paths from `git status` are reindexed directly, and only clean-tree checkout/pull drift falls back to a broader incremental scan. CI remains read-only; set `AI_SEARCH_AUTO_REFRESH=0` to force stale-report-only behavior.

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

### FTS5 Tokenizer (schema_version=3)

`.ai/cache/code.sqlite` `chunks_fts` is created with `tokenize="porter unicode61 remove_diacritics 2"` so that searches match inflected forms (`run` ↔ `running`/`runs`) and accent-stripped variants (`café` ↔ `cafe`). Legacy v2 caches are auto-detected by `init_schema` and rebuilt on first call. Operator action required only if `ai obs search` returns no hits where prior versions did — run `ai index rebuild` once.

### Shell Sandbox (token-cost guard)

For shell commands with potentially large output (`grep`, `find`, `cat`, `tree`, `curl` dumps), Code Brain provides a sandbox that stores full output to disk and returns only a short summary to the model:

```bash
ai exec run --timeout 30 -- grep -rn "useEffect" src/        # short summary
ai exec fetch --exec-id <id> --line-start 100 --line-end 200 # specific range
ai exec fetch --exec-id <id> --grep "useEffect.*deps"        # filter by substring
ai exec list --json                                          # recent executions
ai exec prune --older-than-seconds 86400                     # clean cache (24h default)
```

Storage: `.ai/cache/sandbox/<exec_id>.{txt,meta.json}` (mode `0o600`, gitignored). MCP equivalents: `sandbox_execute`, `sandbox_fetch`, `sandbox_list`. The summary (first 30 lines + last 5 lines, capped 4 KB) replaces a 50–500 KB raw grep dump in the model's context window. `sandbox_execute` is in `WRITE_COMMANDS` and rejected in CI unless explicitly run with the same write policy as `index rebuild`.

### Cross-Session Memory (proactive logging)

Code Brain's central feature is *cross-session context sharing*. The SessionStart hook injects last-N decisions/todos/session notes/resume snapshot into the next session's `additionalContext` — but only if those records exist. Encourage agents to log proactively, and operators can log directly:

| Operation | CLI | MCP tool |
|---|---|---|
| Record decision | `ai memory decision add --text "..." [--tag x]` | `record_decision({text, tags})` |
| Record todo | `ai memory todo add --title "..." [--owner x]` | `record_todo({title, owner, tags})` |
| Close todo | `ai memory todo close --match "<id|substring>"` | `close_todo({match, status, reason})` |
| Append session note | `ai memory session append --text "..."` | `append_session_note({text})` |

All four are write-class (rejected in CI per `WRITE_COMMANDS`). Records auto-redact via `redact_value` before persisting. Decisions are append-only; closing a todo writes a new line with `status="done"` and preserves the original open line for audit.

The leading indicator that the memory layer is being used: `obs usage` shows `hook_breakdown.SessionStart.bytes_total` rising over time. When low, log more aggressively.

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

Code Brain ships four read-only slash commands per agent client plus a project-local MCP server registration. The same backend (`ai obs usage`, `ai obs health-summary`, `ai obs search`, `ai doctor --strict`) is reachable from both Claude Code and Codex CLI.

### Claude Code (`/cb-*`)

Project-level slash commands live under `.claude/commands/cb-*.md`:

| Command | Backend | Read-only |
|---|---|---|
| `/cb-usage` | `ai obs usage --json` (actual transcript tokens; no estimates) | yes |
| `/cb-health` | `ai obs health-summary --json` | yes |
| `/cb-search [query]` | `ai obs search --query "$ARGUMENTS" --json` (exit `13` on stale) | yes |
| `/cb-doctor` | `ai doctor --strict --json` | yes |

Each markdown file forbids the agent from auto-rebuilding the index, auto-running `--refresh-stale`, or fabricating token-saving estimates. The Code Brain doctor `mcp_methods_registered` check fails if any of the four files are missing.

### Codex CLI (`.codex/prompts/cb-*.md`)

The same four operations are mirrored under `.codex/prompts/cb-*.md`. Codex CLI command-registration conventions vary by version — verify your version's prompt-discovery path matches `.codex/prompts/` and adjust `.codex/config.toml` if needed.

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

### PreToolUse Auto-Routing (Claude/Codex)

`.claude/settings.json` (Claude Code) and `.codex/hooks.json` (Codex CLI) register `PreToolUse` hooks that intercept `Bash` tool calls before they execute — this is Code Brain's "auto-routing" of long-output shell commands. `precall.evaluate` decides whether the command would dump large output into the model's context window.

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

Disable: remove the `PreToolUse` block from `.claude/settings.json` (Claude Code) or the `PreToolUse` key from `.codex/hooks.json` (Codex CLI). The `precall` heuristic itself stays loaded but never fires without hook registration.

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
