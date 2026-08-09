# Changelog

All notable Code Brain changes are recorded here.

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
