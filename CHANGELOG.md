# Changelog

All notable Code Brain changes are recorded here.

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
