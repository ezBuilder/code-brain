# Changelog

All notable Code Brain changes are recorded here.

## Unreleased

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
