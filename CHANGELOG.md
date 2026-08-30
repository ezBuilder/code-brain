# Changelog

All notable Code Brain changes are recorded here.

## Unreleased

## 0.9.3 - 2026-08-30

### Changed

- Release packaging now hashes each tracked member while streaming it into the deterministic archive instead of reopening and fully decompressing the finished archive, hashes the archive in bounded chunks instead of loading it all into memory, and uses deterministic gzip level 6 rather than the slower level 9 default. Generated release notes now embed the matching changelog section under an enforced problems-and-fixes heading, and artifact verification requires that body to match the packaged changelog exactly, preventing empty or substituted issue-and-resolution summaries even if provenance is recomputed.
- Default `context_pack` calls now use the lower-cost legacy lexical representation; graph/PPR representations remain available through explicit `v2`, `skeleton`, or `refs-only` selection. The CLI hook path also defers general-command imports and parsing, doctor reports a bounded end-to-end SessionStart entrypoint measurement, and the release gate now rejects sustained or gross hook-latency regressions with an outlier-tolerant end-to-end gate instead of accepting only an in-process proxy.
- Native broad-search routing now permits only demonstrably bounded pipelines and exact `rg` file targets, while compound siblings, shell groups/substitutions, pass-through pagers, oversized caps, and dotted directories remain intercepted. Session context removes duplicate snapshot/live memory and injects only actionable high-confidence lessons.

### Fixed

- Secret redaction now merges assignment spans deterministically, applies path-aware source handling, redacts function-level FTS chunks, invalidates stale scan caches when matcher code changes, and preserves verified Swift type annotations without weakening config-file scanning.
- Pre-tool stream guarding now recognizes nested credential paths and host-specific path aliases, blocks real RSA/DSA/EC/OpenSSH/encrypted private-key headers in write bodies, checks patch targets instead of harmless fixture bodies, and keeps destructive-command and credential-path rules at the executable tool boundary.

## 0.9.2 - 2026-08-29

### Fixed

- The installed test suite now passes in consumer projects. `.ai/runtime/tests` ships into every install, but the source repository's release machinery (`Makefile`, `bootstrap.sh`, `scripts/`, `.github/`, `OPERATIONS.md`, `kits/`) does not, so 12 modules asserting on that machinery failed or aborted collection in an installed project — 18 failures plus a collection error that said nothing about the user's installation, and `test_cli` alone burned over 15 minutes doing it. The runtime now detects the source repository by files a consumer never receives and skips exactly those modules elsewhere, with `collect_ignore` for the module that fails at import time. A lockstep test derives the list from actual source-path references, so a new release-contract test cannot silently reintroduce the failure.

## 0.9.1 - 2026-08-29

### Changed

- `make test` and the release gate now run the suite as parallel per-file shards (`scripts/test-sharded.py`, standard library only, no new dependency), and the gate no longer executes the same suite twice. A full local run drops from about 7m40s to about 3m40s wall time, and the gate stops paying for a duplicate serial pass. `make test-serial` keeps the single-process path. Shards split by file, or by node id only for files proven free of module/session fixtures and ordering marks, and that invariant is asserted by a test.

### Fixed

- `secret_scan` no longer reports self-describing documentation placeholders as credentials. Documenting an environment variable such as an Apple app-specific password or an API key that tells the reader to substitute their own previously failed `doctor --strict` for the repository that documented it, with no fix except an allowlist entry. An exemption now requires all three of: no entropy (single case, no digits, no symbols beyond `-`/`_`), every segment being a known placeholder word, and at least one explicit substitute-me marker. Values built only from descriptive nouns, and anything carrying digits, mixed case, or an unrecognized segment, are still reported.

## 0.9.0 - 2026-08-29

### Added

- Oversized source indexing now streams bounded overlapping windows plus capped symbol spans for Python, Rust, TypeScript, JavaScript, Dart, and Kotlin; generated bundles use path-only classification stubs and every omitted source is reported with a class and reason.
- Host-aware context delivery separates static rules, fingerprinted durable memory, live volatile state, and runtime-only recommendations so Codex does not receive duplicate `AGENTS.md` context while Claude and Antigravity retain their required delivery paths.
- Normal non-CI installs and upgrades now persist trust for the exact Code Brain-managed Codex project hooks after the target transaction commits, with no policy file required; foreign/custom hooks and global user hooks remain review-gated, CI defaults off, and an explicit private policy can extend the exact allowlist.
- Headlong-inspired deterministic episodic memory adds a fanout logarithmic pyramid, hard-budget coverage receipts, stable raw provenance, CLI/MCP context and drill-down surfaces, strict integrity status, and a prebuilt 200-byte `cb-life:` hook cache.
- Lossless digest-named audit segments preserve every raw event while cross-file markers bind path, whole-file hash, last-line hash, and byte count.
- Kiro IDE/CLI v3 receives a standalone five-event hook seed with native plain-stdout/non-zero-exit behavior; CLI v2 is detected and reported as an inert compatibility surface.

### Changed

- Prompt growth, transcript-token refresh, memory page-in, inline auto page-out, memory-tier context, and codegraph hotspot context are explicit opt-ins. Detached sleep-time page-out now refreshes non-destructive audit rollups and the episodic index; raw audit history is authoritative and never auto-deleted. Hook-triggered Git fetch/push and memory sync were removed; cross-machine sync is an explicit `ai memory sync` command or separately managed loop.
- Search results are deduplicated by real source in SQLite before hydration, preventing windows from crowding out other files and avoiding repeated large-file reads per query.
- Managed hook installers now version-gate new Claude/Codex events, set bounded per-event timeouts and Codex context-spill thresholds, cover file-write tools, and preserve foreign hooks across repeated upgrades and downgrades.
- The full current Claude event catalog is documented with explicit non-adoption reasons for duplicate batch/expansion hooks, default-replacing worktree creation, permission auto-approval, and user/MCP elicitation interception; hook count is not treated as a quality metric.

### Fixed

- `scripts/install-check.sh` now waits for the detached children its `SessionStart` hook registers before removing the extraction directory, so a passing archive check can no longer fail the release gate with a `Directory not empty` cleanup race.
- The git-less filesystem baseline no longer presents runtime scratch as tracked source. Exported tarballs, release smoke copies, and consumer checkouts without `.git` previously walked `.ai/tmp` and `.ai/outputs`, so third-party fixtures downloaded into scratch could fail `secret_scan` and strict doctor with findings that do not exist in tracked source.
- The completion guard's shell mutation parser now strips heredoc bodies and tokenizes redirections, so Dart `=>`, Kotlin `->`, and comparison operators inside commands no longer register as write targets and produce false `cb-guard[verification]` stop refusals.
- Large source files no longer disappear silently from code search or symbol/call graphs, and source byte metrics no longer double-count derived windows or symbol chunks.
- Managed `AGENTS.md` currentness now tracks all mirrored feature toggles and bounded nested resume/plan state, preserves opted-in runtime recommendations, fails closed on symlinks, and writes atomically.
- Long-session scope nudges are byte-stable after their threshold, so they no longer defeat UserPromptSubmit delta suppression on every subsequent hook event.
- Hook shims now launch the managed virtualenv Python directly instead of probing it through a second interpreter, cutting duplicate startup work while retaining uv fallback when the environment is absent.
- Codex hook auto-trust now validates Code Brain-owned groups against the installer's shared semantic contract, so preserved foreign groups and version-gated `SessionEnd`/`Interrupt` entries no longer trigger repeated manual review while foreign hashes remain untrusted.
- Managed uninstall now removes only Code Brain's exact Codex project-hook hash entries, retaining pre-existing project trust plus foreign/global hook hashes instead of leaking stale managed state indefinitely.
- Install and upgrade no longer copy source-side `.ai/outputs` reports into every target; one upgrade transaction removes only untracked output paths owned by the previous install manifest while preserving target-created and Git-tracked artifacts.
- Pre-manifest partial Code Brain runtimes can now be upgraded only after three independent managed markers agree, replacing stale managed files while preserving private memory and still rejecting unrelated `.ai` directories.
- Uninstall now removes the Code Brain-owned standalone Kiro hook file while preserving sibling user hooks, preventing dangling commands after the runtime shim is removed.
- Audit folding no longer replaces raw rows, audit rotation no longer discards a tail, old raw years are no longer pruned, and derived storage caps no longer make authoritative memory an impossible reclaim target.
- Audit-chain verification now rejects duplicate IDs/sequences, digest/link/byte-count tampering, and nondeterministic sync branches; explicit repair renames changed segments to their canonical digest and cascades downstream links without dropping content.
- Legacy ID-less event anchors remain stable when a current audit file becomes a segment, and episodic reads refuse mixed concurrent snapshots, duplicate source content, or ambiguous lineage.
- Episodic tiers/meta/cache now fail closed on malformed rows, hash-chain or source-set changes, unsafe files, and any summary/provenance block that cannot be reproduced from raw truth; normal offline builds self-repair disposable corruption without touching raw history.
- Episodic forget tombstones now live outside the reclaimable tier root, preventing storage cleanup from resurrecting forgotten summaries.
- Audit segment sequences now fail closed on missing heads/interior gaps/orphan current markers across doctor, repair, episodic reads, and explicit sync; repair cannot legitimize evidence loss by relinking physical neighbors.
- Raw audit files no longer inherit JSONL union merge. Private-remote sync requires explicit confirmation, transaction-locks and force-stages the complete ignored segment/current set, excludes derived/transient state, and refuses tracked-segment deletion.
- Strict doctor audit/index reads use a transaction-consistent snapshot, failed unbuilt episodic status no longer appears inactive, and known disposable episodic/rollup roots can be reclaimed safely in non-git workspaces while explicit pins remain authoritative.

## 0.8.0 - 2026-08-22

### Added

- Default bounded structural context with exact function spans, typed graph provenance, deterministic one-hop personalized PageRank, and fail-soft stale-edge suppression.
- `ai context prove` plus `/cb-proof` wrappers for legacy/v2 A/B checks, graph activation, determinism, bounded context, latency, and post-warmup retrieval-file no-growth proof.
- Production line-span retrieval evals and the Graft/TurboVec/Semantica adoption record.

### Fixed

- Public and direct upgrades now self-heal bounded `.ai/tmp`/`.ai/outputs` storage and audit-chain splice damage before strict doctor certification.
- Consumer installs no longer fail source-only architecture/global-kit checks; `/kit-doctor` remains the authority for the separately installed global kit.
- Generic secret detection keeps high-signal literals while rejecting ordinary identifier expressions and deterministic repeated-character test placeholders that previously caused false strict-doctor failures.
- Consumer propagation excludes source-owned `.ai/eval` scratch data and preserves project branch, HEAD, and user-owned files.

## 0.7.4 - 2026-08-12

MCP argument contract: malformed tool calls now fail at the schema boundary with an actionable reason instead of returning a soft empty-result body that loop-prone clients retry indefinitely.

### Added

- Required-field enforcement and blank-string rejection before handler dispatch, with `minLength: 1` published for every required string field.
- A bounded repeated-rejection loop guard that escalates after three identical invalid calls and clears after a valid call.
- Client-visible, schema-field-restricted `ToolArgumentError` details that avoid echoing caller-controlled values.

## 0.7.2 - 2026-08-09

Agentic code-retrieval round: live exact search now complements indexed semantic
discovery instead of forcing BM25-first behavior onto every lookup.

### Added

- Unity C# coverage across indexing, exact `rg` fallback, codebase maps, autonomous-harness source detection, and the production retrieval eval axis (`.cs` and `.csx`).
- A measured C# exact-symbol regression where live source must rank ahead of a documentation mention.
- Deep-research evidence and adoption decisions in `.ai/outputs/research-2026-08-09-agentic-code-retrieval.md`.

### Changed

- Exact symbol/path checks route to targeted live-native search; fresh Code Brain retrieval remains the semantic-discovery path, while `context_pack` is on-demand rather than an automatic second call.
- Schema v11 excludes high-churn `.ai/outputs/` artifacts from code indexing and freshness scans. Candidate-cache fingerprints now include all file-policy inputs so policy changes invalidate safely.
- Symbol queries place exact live hits before indexed results, prefer matching source filenames over docs, and keep stale-index warnings visible when auto-refresh is disabled.
- Freshness checks use cheap candidate metadata on every query instead of an mtime gate, detecting newly added source files even when copied with timestamps older than the index.

### Fixed

- Corrupt indexes preserve the `corrupt_index` recovery reason through the new always-on freshness path.
- Older running MCP processes refuse a newer index schema instead of silently lowering `user_version`; restart remains required after an upgrade that changes the index contract.
- `modern-transport-tycoon` C# files are no longer silently absent from both BM25 and exact fallback results.

## 0.7.1 - 2026-08-01

### Fixed

- Structural pre-v2 legacy search indexes now answer deterministically (-012, option A): auto-refresh detects the legacy schema and skips its rebuild instead of silently migrating, so a query fails with the explicit `run ai index rebuild` message regardless of file mtimes. Previously a stale-looking worktree migrated the index from a read path while a fresh-looking one raised — the same query answered two ways. `ai index rebuild` remains the single migration door.
- Hardened for zero steady-state growth and zero added cost: repeated failing legacy queries append no logs, audit rows, locks, or index bytes; the legacy verdict stops after a single hash probe (no second index open); fresh-path queries still run zero probes and never rebuild (regression-gated, happy-path median ~89ms on the source repo).

## 0.7.0 - 2026-08-01

Memory-correctness round: the 2026-08-01 agent-memory deep-research concluded
"build core, borrow concepts, buy none" — every change below hardens Code
Brain's own read/write paths instead of adopting an external memory product.

### Added

- `ai memory forget`: hard-forget a decision/failure by id — appends a tombstone marker and physically compacts the body out of `decisions.jsonl` under the append lock, with a deletion receipt (`removed_rows`, `tombstone_id`, honest `union_merge_restorable: true`). Suppression lives inside the shared `live_decision_records` filter so no reader — including the tail-window SessionStart block — can resurface a forgotten record, and freshly minted ids can never collide with a tombstoned one. CLI-only (`--yes` + `--confirm-id`, CI-rejected); deliberately not exposed over MCP.
- `ai memory forget-note`: remove session-note lines by substring (min 4 chars) and purge `resume.json` snapshots that embed the text, with a receipt.
- `memory_retrieval` eval axis: golden-query Recall@K/MRR gate over the production `recall_memory` pipeline, wired into `make eval` — including liveness cases proving expired, refuted, stale, and tombstoned records never rank.
- `ai reranker status|install|uninstall`: the reranker model finally has a real install surface (the old background spawn targeted a command that never existed).
- Doctor `network_defaults` check: flags stale `AI_SEARCH_*_AUTO_INSTALL` env opt-ins and orphaned model install-locks left behind by the removed background installer.
- MCP temporality parity: `record_decision` accepts `contradicts`/`derives_from`/`expires_at`, `list_decisions` accepts `include_expired`.

### Fixed

- Decision liveness is now enforced on every read path via one shared predicate (`live_decision_records`): HOT-cache consolidation, command drafting, sub-agent recommendation, conflict scanning, resume snapshots, and cross-project federated tag mining all previously leaked expired or refuted decisions back into injected context.
- `expires_at` is validated and UTC-normalized on write; a malformed bound (e.g. `"2026"`) used to expire the record on arrival, silently. Date-only bounds now mean "valid through that day".
- `close_todo` no longer raises `KeyError` on legacy id-less todos; they close under the same derived key the readers use.
- Naive/aware datetime mixing: offset-less timestamps (git-synced or hand-edited stores) raised `TypeError` past fail-soft guards in the SessionStart HOT-cache path, cooldown scanners, observability windows, trajectory summaries, audit folding, and loop expiry; all parse helpers now read offset-less as UTC. Audit rotation also survives `"ts": null/""/garbage` rows instead of aborting.
- `code_query`'s advertised MCP contract (`readOnlyHint`, `openWorldHint: false`) is now truthful in every configuration: dense/rerank activation never spawns background model downloads. With the `[dense]` extras installed, a read-only `code_query` used to trigger a multi-MB fetch.

### Security

- Model downloads are explicit CLI actions only (`ai embedding install`, `ai reranker install`); both command groups joined the CI read-only reject list, and the legacy in-query auto-install (env-default ON) was removed.

## 0.6.6 - 2026-07-30

### Added

- Identifier-subtoken dual-emission indexing (schema v9): camelCase identifiers are now searchable by their split words (e.g. `fetchFlightScheduleBoard` matches "flight schedule board"); disable with `AI_SEARCH_SUBTOKENS=0`. Evidence: identifier-aware BM25 tokenization improves code retrieval by double digits (arXiv:2605.18561).
- Vendored-runtime index exclusion (schema v10): consumer repos no longer index the installed Code Brain payload (`.ai/runtime/`, `.ai/bin/`, `.ai/generated/`, `.ai/evals/`), which previously drowned project code in search results; the source repo opts back in via `search.index_vendored_runtime: true`.
- `code_retrieval` eval axis: golden-query Recall@K/MRR/NDCG@K/latency regression gate over the production `search.query` path, wired into `make eval` (re-lands the deferred retrieval-evaluation follow-up).
- MCP tool annotations (`readOnlyHint`/`destructiveHint`/`idempotentHint`/`openWorldHint`) on curated read-only and write tools, aligning with the 2026 MCP spec and client approval UX; uncertain tools stay unannotated (fail-safe).

### Fixed

- `search.query` now self-heals a version-outdated index (auto rebuild) instead of failing until a manual `ai index rebuild`; structural pre-v2 legacy indexes still require the explicit rebuild and are never dropped on a read path.
- Branch deletion guards now hard-block only protected branch names while allowing worktree, session, feature, and scratch branch cleanup.
- Session start and upgrades now enforce bounded `.ai/tmp`, `.ai/outputs`, and total `.ai` storage with tracked-file and `.keep` preservation.
- Doctor now validates the bounded audit-index tail instead of falsely requiring evicted historical rows.

## 0.6.5 - 2026-07-21

### Added

- Bounded, trust-aware I/O across search, memory, audit, graph, LSP, MCP, and worker paths.
- Storage lifecycle enforcement, private state handling, retention diagnostics, and automatic cleanup.
- Runtime activation and environment diagnostics for installed projects.
- Broader regression coverage for trust boundaries, concurrency, redaction, recovery, and storage limits.

### Changed

- Search indexing, ranking, stemming, and chunking now use bounded, recoverable state.
- Session, doctor, preflight, installer, and upgrade flows now expose clearer operational proof.
- Secret-only fallback searches tolerate platform-specific FTS tokenization while still requiring every returned snippet to remain redacted.

### Fixed

- Streaming transcript parsing bounds memory use for large JSONL histories.
- Runtime and model artifacts reject unsafe paths, oversized inputs, and untrusted state.
- Bootstrap and upgrade scripts consistently activate the selected runtime.

## 0.6.3 - 2026-06-21

- Hid legacy worker-pool and loop surfaces from default discovery while preserving compatibility through the `full-all` profile.
- Pruned retired installed commands without removing user-owned files.
