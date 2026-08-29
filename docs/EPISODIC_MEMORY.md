# Episodic Memory and Lossless Audit History

## Decision

Code Brain adopts the useful invariants from Headlong's logarithmic memory pyramid, pinned for review at [`ed2dac6cfe9a5f2304b6693da34a7102418e2166`](https://github.com/laude-institute/headlong/tree/ed2dac6cfe9a5f2304b6693da34a7102418e2166), without replacing Code Brain's durable decisions, todos, lessons, procedures, or session-resume memory.

The audit trajectory is the authoritative episodic history. Summaries are disposable indexes, never truth.

## Non-negotiable invariants

1. `.ai/memory/audit/*.jsonl` is the source of truth.
2. Raw audit events are not deleted, folded in place, or replaced by summaries.
3. Every new event has a stable `evt-<32 hex>` ID independent of its hash-chain predecessor.
4. Legacy ID-less rows receive deterministic IDs from their logical year/segment lineage, physical line, and normalized content. Moving a current file into its immutable segment therefore does not change its ID.
5. Rollups retain exact half-open ranges, first/last event IDs, bounded anchor IDs, and a full-range SHA-256 provenance digest.
6. Important decisions must drill down to raw rows. A coverage receipt, not a summary, states what was actually included.
7. Hook and MCP hot paths never call a model or the network. `SessionStart` reads only a prebuilt cache capped at 200 UTF-8 bytes.
8. Duplicate segment sequences, duplicate event IDs, digest mismatches, broken links, source shrink, sealed-prefix tampering, and any derived block that cannot be reproduced byte-for-byte from raw truth fail closed.

## Storage layout

```text
.ai/memory/
├── audit/
│   ├── 2026.000001.<sha12>.jsonl   immutable, byte-identical sealed raw segment
│   ├── 2026.000002.<sha12>.jsonl
│   └── 2026.jsonl                  current append target
├── audit-index.jsonl               bounded operational tail index
├── audit-rollups/                   disposable date/action sidecars
├── episodic-tombstones/             authoritative forget markers; never reclaimed with tiers
│   └── audit.jsonl
└── episodic/audit/
    ├── meta.json                    schema, fanout, watermark, sealed-prefix digest
    ├── tier_1.jsonl                 fanout raw-event blocks
    ├── tier_2.jsonl                 fanout tier-1 blocks
    ├── tier_N.jsonl
    └── hook-context.json            prebuilt `cb-life:` cache
```

Before an append would exceed the 64 MB current-file bound, Code Brain seals the current file as `YEAR.SEQUENCE.SHA12.jsonl` without changing a byte. The new current file starts with an independently chained `audit.segment_started` record containing the previous segment path, whole-file SHA-256, last-line SHA-256, byte count, and `lossy:false`.

Each file has its own `prev_sha` chain. Cross-file lineage is verified through the segment marker. A digest-bearing filename is also verified against its content. Discovery is physical and deterministic: year, sequence, digest-name tie-break, then current file. Two files with the same `(year, sequence)` are preserved as evidence but strict doctor and repair both refuse to choose a winner.

## Local/private and cross-machine sync policy

- Raw memory is local-private by default. The repository ignores `.ai/memory/**`, and install/upgrade never copies source-project memory into a consumer or overwrites that consumer's memory.
- Never force-track only `YEAR.jsonl`. A valid synchronized snapshot contains the complete physical `.ai/memory/audit/` set: every immutable segment plus the current file.
- `.ai/.gitattributes` explicitly disables union merge for `memory/audit/*.jsonl`. A divergent current file is a conflict, not data that may be concatenated and re-chained.
- Raw sync is allowed only through the explicit `ai memory sync --private-remote-confirmed` acknowledgement after the operator has verified that the configured Git remote is private. The command holds the audit transaction lock, validates lineage, force-stages the complete ignored authoritative set, excludes locks/derived indexes/queues, and refuses a missing tracked segment.
- A missing first or middle sequence, an orphan current marker, duplicate sequence, digest mismatch, or broken link fails doctor, episodic build/read, repair, and sync. Repair never relinks across an evidence gap; restore the original segment from a private backup.

## Logarithmic pyramid

With fanout 10:

| Tier | Coverage per block |
|---|---:|
| Raw tail | 1 event |
| Tier 1 | 10 events |
| Tier 2 | 100 events |
| Tier 3 | 1,000 events |
| Tier 4 | 10,000 events |

New complete blocks are appended deterministically. Covered lower-tier rows are then canonically compacted while retaining the direct children of the rightmost parent at every tier. That refinement spine preserves useful resolution immediately before the verbatim raw tail while bounding derived rows to `O(fanout × log_fanout(N))`.

Summaries are local extractive text only. They use no LLM, tokenizer, randomness, timestamp, or network call. The same raw source, schema version, prompt version, and fanout produce byte-identical blocks.

## Context and drill-down

`context` emits:

- an explicit `NON-AUTHORITATIVE` warning;
- coarse-to-fine rollup segments;
- the requested recent raw tail;
- a hard UTF-8 byte budget;
- exact `covered` and `uncovered` ranges;
- malformed/non-object raw rows fail closed instead of producing a partial index;
- `source_truth_complete=false` only for explicitly preserved legacy lossy folds.

`drill-down` resolves either one event ID or a half-open global range back to the raw JSON record and its exact source file and line. It is the authoritative read surface.

```bash
.ai/bin/ai memory episodic build --json
.ai/bin/ai memory episodic build --dry-run --json
.ai/bin/ai memory episodic status --json
.ai/bin/ai memory episodic context --byte-budget 8000 --raw-tail 20 --json
.ai/bin/ai memory episodic drill-down --event-id evt-0123456789abcdef0123456789abcdef --json
.ai/bin/ai memory episodic drill-down --start 100 --end 120 --limit 50 --json
```

Read-only MCP tools:

- `episodic_context`: bounded non-authoritative context plus coverage receipt;
- `episodic_drilldown`: authoritative raw event lookup by ID or range.

The normal compact MCP profile discovers them through `tool_search`; it does not inflate the default tool list.

## Lifecycle and performance contract

- Detached memory page-out refreshes the audit index, non-destructive audit rollups, episodic tiers, and hook cache offline.
- Rebuilding an unchanged corpus is a true no-op: tier/meta/cache bytes and mtimes do not change.
- Raw storage is `O(N)` and intentionally retained. Segment count is `O(raw bytes / 64 MB)`.
- Derived episodic rows and prompt-resident context are `O(fanout × log_fanout(N))` and `O(log N)` respectively.
- A build, strict integrity check, explicit context request, or drill-down currently validates/scans the trusted raw corpus in `O(N)`. This cost is kept out of hooks; it is the price of verifying summaries against raw truth without an additional authoritative database.
- The hook path is `O(number of audit files)` metadata checks plus one cache read, bounded to 200 output bytes, with no raw scan and no build.
- If raw audit metadata advances beyond the prebuilt cache watermark, hooks inject only a short stale-index warning. They never reuse an older summary as if it covered the new source.
- Derived episodic data is capped at 128 MB/eight top-level entries. Audit rollups are capped at 64 MB/16 entries. Both are reclaimable and rebuildable; authoritative episodic forget tombstones live outside that reclaim root.
- Authoritative memory bytes are reported but excluded from automatic reclaim limits because deleting them would falsify provenance.

## Failure and repair policy

| Condition | Behavior |
|---|---|
| Same-size first/middle/last sealed-prefix edit | integrity failure; rebuild required |
| Segment content no longer matches filename digest | strict failure |
| Broken segment marker/path/hash/byte count | strict failure |
| Duplicate `(year, sequence)` | deterministic discovery, hard failure, no automatic branch choice |
| Missing first/interior segment or orphan current marker | hard failure everywhere; restore the exact missing raw segment |
| Duplicate event ID | hard failure |
| Malformed or non-object raw row | hard failure; no partial build/context |
| Source changes during a read | fail and retry later; never return a mixed snapshot |
| Derived tier/meta/summary corruption | fail closed, then a normal offline build automatically resets and deterministically rebuilds the disposable index; raw and forget tombstones remain untouched |
| Repair changes a segment's metadata/content | atomically rename it to the new digest name and cascade link updates |

`ai audit repair-chain` only performs an explicit deterministic repair. It never drops a row and refuses divergent duplicate sequences. Restoring an unmodified raw segment from backup is preferable when tampering, rather than a merge splice, is suspected.

## Legacy limitation

Older Code Brain builds destructively replaced some daily raw events with `_folded` records. Those removed events cannot be reconstructed from their counts. The runtime keeps each legacy fold visible, marks `source_truth_complete=false`, and never presents it as recovered truth. The lossless segment and sidecar design prevents future loss; it cannot rewrite history that is already gone.

Decision/failure bodies use their own hard-forget path and never enter audit payloads. Audit records contain IDs/metadata only, so forgetting a decision cannot break the audit chain. Raw audit retention remains provenance retention, not a substitute for semantic decision memory.

## What was deliberately not copied from Headlong

- no lazy LLM summarization;
- no network access in build, hooks, MCP, or doctor;
- no file-per-block layout;
- no first-person synthetic life narrative;
- no claim that summaries are memory truth;
- no unconditional full-life prompt injection.

This keeps Headlong's strongest idea—progressive-resolution recall with raw drill-down—while preserving Code Brain's deterministic, repo-local, multi-agent safety contract.
