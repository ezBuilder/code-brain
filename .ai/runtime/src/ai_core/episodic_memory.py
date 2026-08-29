"""Deterministic episodic memory pyramid (fanout-N rollup tiers).

Headlong-inspired ("logarithmic memory pyramid") but fully local, offline,
and deterministic — no LLM/network calls anywhere in this module. Summaries
are produced by a pure extractive algorithm over event text, never by a
model call, so a rebuild from the same raw source always yields byte-identical
rollups (see ``prompt_version``/``schema_version`` provenance below).

Core design (see docs/decisions and the module docstring sections for detail):

  * Raw source of truth. This module NEVER writes to the caller-provided raw
    event source (e.g. ``.ai/memory/audit/<year>.jsonl``). It only *reads*
    events via an injected iterator/loader. Rollups are an index, not
    testimony: every rollup entry carries an exact raw range, first/last stable
    ids, a full-range content digest, and bounded anchor ids. The exact range
    (or an anchor id) always drills back to raw text.

  * Storage: derived JSONL per tier under
    ``.ai/memory/episodic/<source_key>/tier_<k>.jsonl``. New blocks are appended
    atomically, then redundant covered rows are deterministically compacted
    while retaining the right-frontier refinement spine. These files are
    disposable indexes, not immutable testimony. A tiny ``meta.json`` (atomic
    replace) tracks the incremental build watermark per source.

  * Fanout F (default 10): tier 1 rolls up F raw events; tier k rolls up F
    tier-(k-1) blocks (covers F**k raw events). Blocks are keyed by the
    half-open raw-event-count range ``[start, end)`` they cover, which never
    shifts once sealed (append-only source ⇒ ranges are stable).

  * Deterministic extractive summaries only. No LLM, no network, no
    randomness. A block's ``summary`` is produced by ``_extractive_summary``:
    a stable, order-preserving pick of the most information-dense event
    texts (longest-first tie-broken by original order) joined by " | ",
    hard-truncated to a fixed character budget. Same input bytes -> same
    output bytes, forever, for a given (``schema_version``, ``fanout``).

  * Idempotent, incremental build. ``build()`` reads the current watermark
    from meta.json, does nothing if the source has not grown past the next
    fanout boundary (no-op, no file growth), and only ever appends newly
    completed blocks. Re-running build() on an unchanged source is a true
    no-op (verified by mtime/size unchanged in tests).

  * Legacy id-less rows. Raw events may lack a stable ``id`` field (older
    audit rows). ``stable_event_id()`` falls back to
    ``sha256(namespace + source_line + normalized_text)[:16]`` — deterministic
    for a stable logical source lineage and physical line, so legacy logs still
    get reproducible ids without requiring a schema migration.

  * Staircase assembly with an honest coverage receipt. ``assemble()`` builds
    a raw tail (verbatim, most recent) plus coarse-to-fine rollup segments
    covering everything older, under a **hard byte budget**. It NEVER claims
    full coverage it did not actually include: the returned
    ``CoverageReceipt`` lists exact covered ranges and any ``uncovered``
    ranges that were dropped to stay under budget. Coverage math is
    exact/no-gap/no-overlap by construction (segments partition
    ``[0, total)``) and is asserted by the receipt itself, not implied.

  * Drill-down. ``drill_down(event_id=...)`` or ``drill_down(range=(s, e))``
    resolves back to raw event rows via the stable id index, and
    ``resolve_citations()`` maps a block's ``event_ids`` back to raw text.

  * Tombstones / staleness. ``tombstone_range()`` marks a raw range as
    explicitly forgotten (private, append-only tombstone log); any rollup
    block whose range intersects a tombstoned range is treated as stale by
    ``is_stale()`` and excluded from ``assemble()`` unless
    ``include_stale=True``. If the raw source ever shrinks (size/line-count
    decreases versus the recorded watermark — should not happen for
    append-only logs, but is detected defensively), the whole per-source
    cache is treated as stale and a full rebuild is required; ``build()``
    surfaces this via ``SourceShrinkError`` rather than silently trusting a
    now-invalid watermark.

No filesystem writes happen at import time. All writes go through
``ai_core.private_write`` (atomic replace / append-with-lock), matching the
same primitives ``ai_core.memory`` already uses for ``.ai/memory/*.jsonl``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .private_write import (
    append_private_text,
    atomic_write_private_text,
    list_root_confined_directory,
    private_file_lock,
    read_root_confined_text,
    unlink_root_confined_regular_file,
)

# ---------------------------------------------------------------------------
# Versioning / provenance constants
# ---------------------------------------------------------------------------

#: Bumped when the on-disk block JSON shape changes incompatibly.
SCHEMA_VERSION = 1

#: Bumped when the deterministic extractive-summary algorithm changes, so a
#: stale block (built by an older algorithm) is detectable rather than
#: silently trusted. Never bumped for pure performance changes that keep
#: identical output.
PROMPT_VERSION = 1

#: Default fanout F: events per tier-1 block; children per higher tier.
DEFAULT_FANOUT = 10

#: Hard per-block summary character budget (extractive, not tokens — this
#: module has no tokenizer dependency).
DEFAULT_SUMMARY_CHARS = 480

#: Max event ids carried per block (anchors for drill-down).
MAX_ANCHOR_IDS = 6


class EpisodicMemoryError(Exception):
    """Base class for episodic-memory errors."""


class SourceShrinkError(EpisodicMemoryError):
    """Raised when the raw source has fewer events than the recorded watermark.

    Append-only sources should never shrink; if one does, the previously
    sealed ranges may no longer correspond to the same raw content, so the
    cache for this source is untrustworthy and must be rebuilt from scratch
    (``build(..., force_rebuild=True)``) rather than silently extended.
    """


class SourceTamperError(EpisodicMemoryError):
    """Raised when the raw source's event count matches the watermark but its
    content at an already-sealed position no longer matches.

    A count-only watermark cannot detect a non-append mutation that
    preserves length (e.g. an in-place edit or reorder of already-sealed raw
    events). This is detected by recomputing the chained digest of every
    complete tier-1 range in the sealed prefix — a mismatch means the source
    was mutated in a way this cache's provenance cannot trust, and
    ``build(..., force_rebuild=True)`` is required.
    """


class IndexIntegrityError(EpisodicMemoryError):
    """Raised when a disposable derived index is malformed or source-inconsistent."""


class BudgetTooSmallError(EpisodicMemoryError):
    """Raised when the assemble byte budget cannot fit even the raw tail header."""


# ---------------------------------------------------------------------------
# Raw event model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RawEvent:
    """One raw source record, as seen by this module.

    ``index`` is the zero-based *sequential ordinal* of this event within
    the ``events`` sequence passed to ``build()``/``assemble()`` — i.e. it
    always equals the event's position in that list (``events[i].index ==
    i`` for any list built by this module's own loader or by a caller
    following this contract). This is required so that ``Block.start``/
    ``Block.end`` (which are list-position ranges: ``events[start:end]``)
    always mean the same thing as ``drill_down(range_=(start, end))``
    (which filters by ``event.index``). If a caller instead used the raw
    on-disk *line number* as ``index`` (which can have gaps when corrupt or
    blank lines are skipped), ``Block`` ranges and ``drill_down`` ranges
    would silently disagree — see ``source_line`` below for where the
    physical line number actually belongs.

    ``source_line`` is the physical line number (or other stable on-disk
    position) this event came from in its original source, independent of
    how many prior lines were skipped as malformed/blank. It is used only
    for legacy-id derivation (see ``stable_event_id``) and provenance — a
    corrupt/blank line inserted *upstream* of existing valid rows changes
    line numbers but must never change the ordinal ``index`` of rows that
    already have sealed rollup blocks pointing at them by *ordinal*
    position; keying the legacy-id fallback off ``source_line`` instead of
    ``index`` keeps already-hashed legacy ids stable across such edits as
    long as each row's own physical line does not move.

    ``event_id`` is either the source's own stable id (if present) or a
    deterministic fallback derived from ``source_line`` + normalized
    ``text`` (legacy id-less rows).
    """

    index: int
    event_id: str
    text: str
    raw: dict[str, Any]
    source_line: int = -1

    def __post_init__(self) -> None:
        if self.source_line < 0:
            object.__setattr__(self, "source_line", self.index)


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, sort_keys=True, ensure_ascii=True)
        except (TypeError, ValueError):
            text = str(value)
    # Collapse whitespace deterministically so summaries are stable across
    # platforms/newline styles.
    return re.sub(r"\s+", " ", text).strip()


def stable_event_id(
    index: int,
    raw: dict[str, Any],
    *,
    id_field: str = "id",
    namespace: str = "",
    source_line: int | None = None,
) -> str:
    """Return a stable id for one raw record.

    Prefers an existing stable id field (``id``, or the given ``id_field``)
    when present and non-empty. Falls back to a deterministic hash of
    ``namespace`` + a stable position key + normalized text for legacy rows
    that carry no id — the same (namespace, position, content) always
    yields the same fallback id, so idless logs are still drillable and
    reproducible.

    The position key used in the fallback hash is ``source_line`` when
    given, else ``index``. Callers reading from an on-disk source with
    possibly-skipped corrupt/blank lines (see ``load_jsonl_events``) should
    pass the *physical line number* as ``source_line`` so that a
    corrupt/blank line inserted upstream of already-sealed rows never
    changes their fallback id (which would happen if the sequential
    ordinal ``index`` were used instead, since that shifts when earlier
    lines are skipped).

    ``namespace`` (typically the source name) prevents legacy-id collisions
    when events from two different sources happen to share the same
    position and text; the default ``""`` reproduces the exact hash used
    before this parameter existed, so already-sealed blocks for existing
    single-source callers remain valid.
    """
    for candidate_field in (id_field, "id", "step_id", "event_id"):
        candidate = raw.get(candidate_field)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    text = _normalize_text(
        raw.get("text")
        or raw.get("content")
        or raw.get("message")
        or raw.get("summary")
        or raw
    )
    prefix = f"{namespace}:" if namespace else ""
    position = index if source_line is None else source_line
    digest = hashlib.sha256(f"{prefix}{position}:{text}".encode("utf-8")).hexdigest()
    return f"legacy:{digest[:16]}"


def _event_text(raw: dict[str, Any]) -> str:
    for candidate_field in ("text", "content", "message", "summary", "note"):
        value = raw.get(candidate_field)
        if isinstance(value, str) and value.strip():
            return _normalize_text(value)
    return _normalize_text(raw)


def load_jsonl_events(path: Path, *, namespace: str | None = None) -> list[RawEvent]:
    """Read-only loader for a JSONL raw source (e.g. an audit log).

    Malformed lines are skipped (never raise), matching the tolerant
    ``fromjson?``-style parsing convention used elsewhere in this repo for
    append-only logs that may have a torn trailing line. This function
    never writes to ``path``.

    ``RawEvent.index`` returned here is the *sequential ordinal* of each
    successfully-parsed row (0, 1, 2, ... with no gaps) — i.e. it always
    equals the row's position in the returned list. This is required
    because ``build()`` records ``Block.start``/``Block.end`` as ranges
    over list position (``events[start:end]``), and ``drill_down(range_=
    ...)`` filters by ``event.index``; if ``index`` instead used the raw
    on-disk *line number* (which can have gaps whenever a corrupt/blank
    line is skipped), a sealed block's ``[start, end)`` range would no
    longer correspond to the same events under ``drill_down`` once any
    line had ever been skipped.

    The physical on-disk line number is preserved separately as
    ``RawEvent.source_line`` (blank/corrupt lines still consume a line
    number) and is what actually anchors the legacy id-less fallback hash
    (via ``stable_event_id(..., source_line=...)``) — so a later
    corrupt/blank line inserted upstream of existing valid rows changes
    those rows' sequential ``index`` (as it must, to keep ranges list-
    position-correct) but never changes their fallback id, since the
    fallback hash keys off ``source_line``, not ``index``.

    ``namespace`` defaults to the file's stem and is passed to
    ``stable_event_id`` so legacy (id-less) rows from different files never
    collide on their fallback hash.
    """
    events: list[RawEvent] = []
    if not path.exists():
        return events
    ns = path.stem if namespace is None else namespace
    ordinal = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(raw, dict):
                continue
            events.append(
                RawEvent(
                    index=ordinal,
                    event_id=stable_event_id(
                        ordinal, raw, namespace=ns, source_line=line_number
                    ),
                    text=_event_text(raw),
                    raw=raw,
                    source_line=line_number,
                )
            )
            ordinal += 1
    return events


# ---------------------------------------------------------------------------
# Deterministic extractive summarizer (NO LLM, NO network)
# ---------------------------------------------------------------------------


def _extractive_summary(
    texts: Sequence[str], *, max_chars: int = DEFAULT_SUMMARY_CHARS
) -> str:
    """Pick the most information-dense texts, deterministically, no LLM.

    Heuristic: prefer longer, non-empty texts (more likely to carry signal
    than short noise like "ok"), tie-broken by original chronological order
    to keep ties reproducible. Picked texts are re-sorted back into
    chronological order before joining, then hard-truncated to
    ``max_chars`` bytes-of-text (character count; this module has no token
    dependency) with an explicit truncation marker so callers can tell a
    summary was clipped versus naturally short.
    """
    scored = [
        (i, text) for i, text in enumerate(texts) if text and text.strip()
    ]
    if not scored:
        return ""
    ranked = sorted(scored, key=lambda pair: (-len(pair[1]), pair[0]))
    budget_count = max(1, min(len(ranked), MAX_ANCHOR_IDS))
    picked = sorted(ranked[:budget_count], key=lambda pair: pair[0])
    joined = " | ".join(text for _, text in picked)
    if len(joined) <= max_chars:
        return joined
    truncated = joined[: max(0, max_chars - 1)].rstrip()
    return truncated + "\u2026"  # deterministic single-char ellipsis marker


def _extractive_summary_from_child_summaries(
    summaries: Sequence[str], *, max_chars: int = DEFAULT_SUMMARY_CHARS
) -> str:
    """Roll a tier's child summaries up one level (still no LLM)."""
    return _extractive_summary(list(summaries), max_chars=max_chars)


# ---------------------------------------------------------------------------
# Block model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Block:
    """One sealed rollup block: tier k, half-open raw-index range [start, end).

    Provenance fields let a caller (or ``build()`` itself, on a later run)
    verify that a sealed block still corresponds to the exact raw content
    it was built from, independent of the count-only watermark check:

      * ``first_event_id`` / ``last_event_id`` — the stable ids of the
        first and last raw event in ``[start, end)`` at seal time. For a
        tier-1 block these are drawn directly from the raw events; for a
        higher tier they are the first/last event id of the first/last
        child block (i.e. still ultimately anchored to raw event ids, not
        synthesized).
      * ``raw_sha256`` — a canonical, order-preserving digest over every
        raw event id + normalized text in ``[start, end)`` (tier 1) or
        every child block's own ``raw_sha256`` in order (higher tiers).
        Two blocks with the same ``raw_sha256`` are guaranteed to have been
        built from byte-identical underlying raw content; a mismatch on
        rebuild is a positive tamper signal even for content in the
        *middle* or at the *end* of an already-sealed range, not just its
        first anchor (see ``raw_range_digest`` / ``SourceTamperError``).
    """

    tier: int
    start: int
    end: int
    summary: str
    themes: tuple[str, ...]
    event_ids: tuple[str, ...]
    schema_version: int
    prompt_version: int
    fanout: int
    source_digest: str
    first_event_id: str = ""
    last_event_id: str = ""
    raw_sha256: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "start": self.start,
            "end": self.end,
            "summary": self.summary,
            "themes": list(self.themes),
            "event_ids": list(self.event_ids),
            "schema_version": self.schema_version,
            "prompt_version": self.prompt_version,
            "fanout": self.fanout,
            "source_digest": self.source_digest,
            "first_event_id": self.first_event_id,
            "last_event_id": self.last_event_id,
            "raw_sha256": self.raw_sha256,
        }

    @staticmethod
    def from_json(payload: dict[str, Any]) -> "Block":
        return Block(
            tier=int(payload["tier"]),
            start=int(payload["start"]),
            end=int(payload["end"]),
            summary=str(payload.get("summary", "")),
            themes=tuple(payload.get("themes", []) or []),
            event_ids=tuple(payload.get("event_ids", []) or []),
            schema_version=int(payload.get("schema_version", 0)),
            prompt_version=int(payload.get("prompt_version", 0)),
            fanout=int(payload.get("fanout", DEFAULT_FANOUT)),
            source_digest=str(payload.get("source_digest", "")),
            first_event_id=str(payload.get("first_event_id", "") or ""),
            last_event_id=str(payload.get("last_event_id", "") or ""),
            raw_sha256=str(payload.get("raw_sha256", "") or ""),
        )


def _derive_themes(texts: Sequence[str], *, limit: int = 4) -> tuple[str, ...]:
    """Deterministic keyword extraction: most frequent word tokens, ties broken
    by first-seen order, lowercased, stripped of short/common stop tokens.
    Purely local string processing — not an LLM call.
    """
    stop = {
        "the", "a", "an", "and", "or", "of", "to", "in", "is", "for", "on",
        "with", "this", "that", "it", "was", "were", "are", "be", "at",
    }
    counts: dict[str, int] = {}
    order: list[str] = []
    for text in texts:
        for word in re.findall(r"[a-zA-Z][a-zA-Z0-9_\-]{2,}", text.lower()):
            if word in stop:
                continue
            if word not in counts:
                order.append(word)
            counts[word] = counts.get(word, 0) + 1
    ranked = sorted(order, key=lambda w: (-counts[w], order.index(w)))
    return tuple(ranked[:limit])


def raw_range_digest(event_ids: Sequence[str], texts: Sequence[str]) -> str:
    """Canonical, order-preserving digest over a raw event range.

    Deterministic given the exact ordered sequence of ``(event_id, text)``
    pairs — used as ``Block.raw_sha256`` for tier-1 blocks. Two calls with
    identical inputs always produce the same digest; any change to an id,
    a text, or the order changes it. This is intentionally a pure content
    hash (no LLM, no randomness, no timestamps).
    """
    hasher = hashlib.sha256()
    for event_id, text in zip(event_ids, texts):
        hasher.update(event_id.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(text.encode("utf-8"))
        hasher.update(b"\x01")
    return hasher.hexdigest()


def _child_range_digest(child_digests: Sequence[str]) -> str:
    """Canonical digest for a higher-tier block from its children's own
    ``raw_sha256`` values, in order — chains all the way down to raw
    content without re-reading raw text at every tier.
    """
    hasher = hashlib.sha256()
    for digest in child_digests:
        hasher.update(digest.encode("utf-8"))
        hasher.update(b"\x02")
    return hasher.hexdigest()


def _sealed_prefix_digest(tier1_blocks: Sequence[Block]) -> str:
    """Chain every sealed tier-1 block's ``raw_sha256`` (in start order)
    into a single digest covering the *entire* sealed watermark prefix —
    not just the earliest block's first anchor. Any count-preserving
    mutation anywhere in the already-sealed prefix (first, middle, or last
    block) changes this digest, so ``build()`` can detect tamper at any
    position, not only at raw index 0.
    """
    hasher = hashlib.sha256()
    for block in sorted(tier1_blocks, key=lambda b: b.start):
        hasher.update(block.raw_sha256.encode("utf-8"))
        hasher.update(b"\x03")
    return hasher.hexdigest()


# ---------------------------------------------------------------------------
# Storage layout
# ---------------------------------------------------------------------------


def _source_key(name: str) -> str:
    raw = str(name)
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", raw).strip("._") or "source"
    if safe != raw or len(safe) > 80:
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        safe = f"{safe[:63]}-{digest}"
    return safe


def episodic_dir(root: Path, source_name: str) -> Path:
    return Path(root) / ".ai" / "memory" / "episodic" / _source_key(source_name)


def _tier_path(root: Path, source_name: str, tier: int) -> Path:
    return episodic_dir(root, source_name) / f"tier_{tier}.jsonl"


def _meta_path(root: Path, source_name: str) -> Path:
    return episodic_dir(root, source_name) / "meta.json"


def _tombstone_path(root: Path, source_name: str) -> Path:
    # Forget markers are authoritative privacy state, not a disposable tier.
    # Keeping them outside `.ai/memory/episodic/` prevents storage reclamation
    # from deleting a forget request and later resurfacing a stale summary.
    return (
        Path(root)
        / ".ai"
        / "memory"
        / "episodic-tombstones"
        / f"{_source_key(source_name)}.jsonl"
    )


def _lock_path(root: Path, source_name: str) -> Path:
    return episodic_dir(root, source_name) / ".build.lock"


def _read_meta(root: Path, source_name: str) -> dict[str, Any]:
    path = _meta_path(root, source_name)
    try:
        text, _state = read_root_confined_text(
            path,
            root=root,
            max_bytes=65_536,
            require_private=True,
            require_owner=True,
            reject_group_other_writable=True,
        )
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        raise IndexIntegrityError(f"unreadable episodic metadata: {type(exc).__name__}") from exc
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise IndexIntegrityError("invalid episodic metadata JSON") from exc
    if not isinstance(payload, dict):
        raise IndexIntegrityError("episodic metadata must be an object")
    return payload


def _write_meta(root: Path, source_name: str, meta: dict[str, Any]) -> None:
    path = _meta_path(root, source_name)
    atomic_write_private_text(path, json.dumps(meta, sort_keys=True) + "\n", root=root)


def _source_digest(source_name: str, fanout: int) -> str:
    return hashlib.sha256(f"{source_name}:{fanout}:{SCHEMA_VERSION}".encode("utf-8")).hexdigest()[:16]


def _read_tier_blocks(root: Path, source_name: str, tier: int) -> list[Block]:
    path = _tier_path(root, source_name, tier)
    try:
        text, _state = read_root_confined_text(
            path,
            root=root,
            max_bytes=100_000_000,
            require_private=True,
            require_owner=True,
            reject_group_other_writable=True,
        )
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as exc:
        raise IndexIntegrityError(f"unreadable episodic tier {tier}: {type(exc).__name__}") from exc
    blocks: list[Block] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise TypeError("tier row is not an object")
            block = Block.from_json(payload)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise IndexIntegrityError(f"invalid episodic tier {tier} row {line_number}") from exc
        if block.tier != tier:
            raise IndexIntegrityError(
                f"episodic tier file/row mismatch: file={tier} row={block.tier}"
            )
        blocks.append(block)
    ranges = [(block.start, block.end) for block in blocks]
    if ranges != sorted(ranges) or len(ranges) != len(set(ranges)):
        raise IndexIntegrityError(f"episodic tier {tier} ranges are unordered or duplicated")
    return blocks


def _append_block(root: Path, source_name: str, block: Block) -> None:
    path = _tier_path(root, source_name, block.tier)
    with private_file_lock(path.with_suffix(".jsonl.lock"), root=root):
        append_private_text(path, json.dumps(block.to_json(), sort_keys=True) + "\n", root=root)


# ---------------------------------------------------------------------------
# Tombstones / staleness
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tombstone:
    start: int
    end: int
    reason: str


def read_tombstones(root: Path, source_name: str) -> list[Tombstone]:
    path = _tombstone_path(root, source_name)
    try:
        text, _state = read_root_confined_text(
            path,
            root=root,
            max_bytes=16_000_000,
            require_private=True,
            require_owner=True,
            reject_group_other_writable=True,
        )
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as exc:
        raise IndexIntegrityError(f"unreadable episodic tombstones: {type(exc).__name__}") from exc
    out: list[Tombstone] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise TypeError("tombstone row is not an object")
            tombstone = Tombstone(
                start=int(payload["start"]),
                end=int(payload["end"]),
                reason=str(payload.get("reason", "")),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise IndexIntegrityError(f"invalid episodic tombstone row {line_number}") from exc
        if tombstone.start < 0 or tombstone.end <= tombstone.start:
            raise IndexIntegrityError(f"invalid episodic tombstone range at row {line_number}")
        out.append(tombstone)
    return out


def tombstone_range(
    root: Path, source_name: str, *, start: int, end: int, reason: str = ""
) -> None:
    """Explicitly mark raw range [start, end) as forgotten.

    This is append-only (a private, audit-able log of forget requests) —
    it does not delete or rewrite sealed blocks. Any block whose range
    intersects a tombstoned range is treated as stale by ``is_stale()``.
    """
    if start < 0 or end <= start:
        raise ValueError("tombstone range must be non-empty (end > start)")
    path = _tombstone_path(root, source_name)
    bounded_reason = re.sub(r"\s+", " ", str(reason)).strip()[:512]
    record = {"start": int(start), "end": int(end), "reason": bounded_reason}
    with private_file_lock(path.with_suffix(".jsonl.lock"), root=root):
        append_private_text(path, json.dumps(record, sort_keys=True) + "\n", root=root)


def _ranges_intersect(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start < b_end and b_start < a_end


def is_stale(block: Block, tombstones: Sequence[Tombstone]) -> bool:
    """A block is stale if it does not match current provenance, or if its
    raw range intersects any tombstoned range.
    """
    if block.schema_version != SCHEMA_VERSION or block.prompt_version != PROMPT_VERSION:
        return True
    for tomb in tombstones:
        if _ranges_intersect(block.start, block.end, tomb.start, tomb.end):
            return True
    return False


def _recompute_sealed_prefix_digest(
    events: Sequence["RawEvent"], *, watermark: int, fanout: int
) -> str:
    """Recompute the tier-1 ``raw_sha256`` chain over ``events[0:watermark]``
    directly from the live events, using the exact same fanout-sized
    window boundaries ``build()`` uses to seal tier-1 blocks.

    This intentionally never reads sealed block files from disk: it must
    still detect tamper (and still work) even after compaction has removed
    the on-disk tier-1 blocks for old, fully-rolled-up ranges, since the
    live raw events are the only thing this check can trust the digest
    against. Only whole fanout-sized windows within ``[0, watermark)`` are
    included, matching exactly what was sealed; a partial trailing window
    is never part of the sealed prefix.
    """
    hasher = hashlib.sha256()
    next_start = 0
    while next_start + fanout <= watermark:
        end = next_start + fanout
        window = events[next_start:end]
        digest = raw_range_digest(
            [event.event_id for event in window],
            [event.text for event in window],
        )
        hasher.update(digest.encode("utf-8"))
        hasher.update(b"\x03")
        next_start = end
    return hasher.hexdigest()


_TIER_FILE_RE = re.compile(r"^tier_([1-9][0-9]*)\.jsonl$")
_MAX_TIER_FILES = 64


def _tier_file_tiers(root: Path, source_name: str) -> list[int]:
    """Return canonical tier numbers through confined, bounded discovery."""

    directory = episodic_dir(root, source_name)
    try:
        names = list_root_confined_directory(
            directory,
            root=Path(root),
            max_entries=256,
        )
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise IndexIntegrityError(f"unreadable episodic directory: {type(exc).__name__}") from exc
    tiers: list[int] = []
    for name in names:
        match = _TIER_FILE_RE.fullmatch(name)
        if match is None:
            continue
        tier = int(match.group(1))
        if tier > _MAX_TIER_FILES:
            raise IndexIntegrityError("episodic tier number exceeds safety bound")
        tiers.append(tier)
    if len(tiers) != len(set(tiers)):
        raise IndexIntegrityError("duplicate episodic tier files")
    return sorted(tiers)


def _remove_disposable_index(root: Path, source_name: str) -> None:
    """Invalidate metadata first, then delete only known confined derived files."""

    directory = episodic_dir(root, source_name)
    try:
        names = list_root_confined_directory(
            directory,
            root=Path(root),
            max_entries=256,
        )
    except FileNotFoundError:
        return
    except OSError as exc:
        raise IndexIntegrityError(f"cannot reset episodic directory: {type(exc).__name__}") from exc
    ordered = ["meta.json", "hook-context.json"] + sorted(
        name for name in names if _TIER_FILE_RE.fullmatch(name)
    )
    for name in ordered:
        try:
            unlink_root_confined_regular_file(directory / name, root=Path(root))
        except OSError as exc:
            raise IndexIntegrityError(f"cannot reset episodic derived file: {name}") from exc


def _validate_event_sequence(events: Sequence[RawEvent]) -> None:
    seen: set[str] = set()
    for index, event in enumerate(events):
        if event.index != index:
            raise IndexIntegrityError(
                f"raw event ordinal mismatch: position={index} event.index={event.index}"
            )
        if not isinstance(event.event_id, str) or not event.event_id:
            raise IndexIntegrityError(f"raw event {index} has no stable id")
        if event.event_id in seen:
            raise IndexIntegrityError(f"duplicate raw event id at position {index}")
        seen.add(event.event_id)


def _expected_block(
    events: Sequence[RawEvent],
    source_name: str,
    *,
    fanout: int,
    tier: int,
    start: int,
    memo: dict[tuple[int, int], Block],
) -> Block:
    """Re-derive one block from raw truth for disposable-index validation."""

    key = (tier, start)
    cached = memo.get(key)
    if cached is not None:
        return cached
    if tier < 1:
        raise IndexIntegrityError("episodic block tier must be positive")
    span = fanout**tier
    end = start + span
    if start < 0 or start % span != 0 or end > len(events):
        raise IndexIntegrityError(
            f"episodic block range is not canonical: tier={tier} range=[{start},{end})"
        )
    source_digest = _source_digest(source_name, fanout)
    if tier == 1:
        window = events[start:end]
        texts = [event.text for event in window]
        ids = [event.event_id for event in window]
        expected = Block(
            tier=tier,
            start=start,
            end=end,
            summary=_extractive_summary(texts),
            themes=_derive_themes(texts),
            event_ids=tuple(ids[:MAX_ANCHOR_IDS]),
            schema_version=SCHEMA_VERSION,
            prompt_version=PROMPT_VERSION,
            fanout=fanout,
            source_digest=source_digest,
            first_event_id=ids[0],
            last_event_id=ids[-1],
            raw_sha256=raw_range_digest(ids, texts),
        )
    else:
        child_span = fanout ** (tier - 1)
        children = [
            _expected_block(
                events,
                source_name,
                fanout=fanout,
                tier=tier - 1,
                start=child_start,
                memo=memo,
            )
            for child_start in range(start, end, child_span)
        ]
        child_summaries = [child.summary for child in children]
        anchors: list[str] = []
        for child in children:
            anchors.extend(child.event_ids)
        expected = Block(
            tier=tier,
            start=start,
            end=end,
            summary=_extractive_summary_from_child_summaries(child_summaries),
            themes=_derive_themes(child_summaries),
            event_ids=tuple(anchors[:MAX_ANCHOR_IDS]),
            schema_version=SCHEMA_VERSION,
            prompt_version=PROMPT_VERSION,
            fanout=fanout,
            source_digest=source_digest,
            first_event_id=children[0].first_event_id,
            last_event_id=children[-1].last_event_id,
            raw_sha256=_child_range_digest([child.raw_sha256 for child in children]),
        )
    memo[key] = expected
    return expected


def validate_index(
    root: Path,
    source_name: str,
    events: Sequence[RawEvent],
    *,
    fanout: int = DEFAULT_FANOUT,
) -> dict[str, Any]:
    """Validate every stored rollup against deterministic raw-source truth.

    This is intentionally an offline/explicit O(N) check. Resident hook context
    remains O(log N) and reads only its tiny prebuilt cache.
    """

    if fanout < 2:
        raise ValueError("fanout must be >= 2")
    root = Path(root)
    _validate_event_sequence(events)
    meta = _read_meta(root, source_name)
    tiers = _tier_file_tiers(root, source_name)
    if not meta:
        if tiers:
            raise IndexIntegrityError("episodic tiers exist without metadata")
        return {"built": False, "tier_files": 0, "tier_rows": 0, "index_bytes": 0}

    expected_meta = {
        "fanout": fanout,
        "schema_version": SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "source_digest": _source_digest(source_name, fanout),
    }
    for key, expected in expected_meta.items():
        if meta.get(key) != expected:
            raise IndexIntegrityError(f"episodic metadata mismatch: {key}")
    try:
        watermark = int(meta["watermark"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise IndexIntegrityError("episodic metadata watermark is invalid") from exc
    if watermark < 0 or watermark > len(events):
        raise IndexIntegrityError("episodic metadata watermark is out of range")
    expected_prefix = _recompute_sealed_prefix_digest(
        events,
        watermark=watermark,
        fanout=fanout,
    )
    if meta.get("sealed_prefix_digest") != expected_prefix:
        raise IndexIntegrityError("episodic sealed-prefix digest mismatch")

    memo: dict[tuple[int, int], Block] = {}
    tier_rows = 0
    index_bytes = 0
    for tier in tiers:
        path = _tier_path(root, source_name, tier)
        blocks = _read_tier_blocks(root, source_name, tier)
        try:
            _text, state = read_root_confined_text(
                path,
                root=root,
                max_bytes=100_000_000,
                require_private=True,
                require_owner=True,
                reject_group_other_writable=True,
            )
        except (OSError, UnicodeDecodeError) as exc:
            raise IndexIntegrityError(f"unreadable episodic tier {tier}") from exc
        index_bytes += int(state.st_size)
        for block in blocks:
            expected = _expected_block(
                events,
                source_name,
                fanout=fanout,
                tier=tier,
                start=block.start,
                memo=memo,
            )
            if block != expected:
                raise IndexIntegrityError(
                    f"episodic block differs from raw truth: tier={tier} "
                    f"range=[{block.start},{block.end})"
                )
            tier_rows += 1
    return {
        "built": True,
        "watermark": watermark,
        "tier_files": len(tiers),
        "tier_rows": tier_rows,
        "index_bytes": index_bytes,
    }


# ---------------------------------------------------------------------------
# Build (incremental, idempotent)
# ---------------------------------------------------------------------------


@dataclass
class BuildResult:
    sealed_tier1: int
    sealed_higher: int
    total_events: int
    fanout: int
    no_op: bool

    @property
    def sealed_total(self) -> int:
        return self.sealed_tier1 + self.sealed_higher


def build(
    root: Path,
    source_name: str,
    events: Sequence[RawEvent],
    *,
    fanout: int = DEFAULT_FANOUT,
    max_tiers: int | None = None,
    force_rebuild: bool = False,
) -> BuildResult:
    """Incrementally seal newly-complete rollup blocks for ``source_name``.

    Idempotent: calling this twice in a row with an unchanged ``events``
    sequence performs zero writes on the second call (``no_op=True``,
    verified by tests asserting file mtimes/sizes are unchanged). New blocks
    are appended whole, then redundant derived rows may be canonically
    compacted. Crash-safe: if a previous build died mid-way, resuming
    re-derives missing blocks without duplicating authoritative raw data.

    Raises ``SourceShrinkError`` if the recorded watermark exceeds the
    current event count (append-only sources should never shrink) unless
    ``force_rebuild=True``, in which case the per-source cache directory is
    dropped and rebuilt from scratch.
    """
    if fanout < 2:
        raise ValueError("fanout must be >= 2")
    root = Path(root)
    _validate_event_sequence(events)
    total = len(events)

    with private_file_lock(_lock_path(root, source_name), root=root):
        if force_rebuild:
            _remove_disposable_index(root, source_name)
            meta: dict[str, Any] = {}
        else:
            meta = _read_meta(root, source_name)
        try:
            watermark = int(meta.get("watermark", 0) or 0)
            meta_fanout = int(meta.get("fanout", fanout) or fanout)
        except (TypeError, ValueError, OverflowError) as exc:
            raise IndexIntegrityError("episodic metadata contains invalid numeric fields") from exc
        stored_prefix_digest = str(meta.get("sealed_prefix_digest", "") or "")

        shrunk = watermark > total
        fanout_changed = meta_fanout != fanout

        if shrunk and not force_rebuild:
            raise SourceShrinkError(
                f"source '{source_name}' shrank: watermark={watermark} > total={total}"
            )
        if fanout_changed and not force_rebuild:
            # Fanout change invalidates the pyramid shape; rebuild is
            # required rather than silently mixing shapes across tiers.
            raise EpisodicMemoryError(
                f"fanout changed ({meta_fanout} -> {fanout}) for source "
                f"'{source_name}'; call build(force_rebuild=True) to rebuild"
            )

        if not shrunk and not fanout_changed and not force_rebuild:
            # Count-preserving tamper check over the *entire* sealed
            # prefix, not just the earliest block's first anchor: recompute
            # what the tier-1 digest chain would be for the live events
            # currently occupying every already-sealed tier-1 range
            # (whether those tier-1 blocks are still on disk or were
            # compacted away in favor of a coarser sealed parent — either
            # way the exact same raw_sha256 values must reproduce), and
            # compare against the digest recorded at seal time. A mismatch
            # anywhere in [0, watermark) — first, middle, or last block —
            # is caught, unlike a check that only re-examines index 0.
            if stored_prefix_digest and watermark > 0:
                live_prefix_digest = _recompute_sealed_prefix_digest(
                    events, watermark=watermark, fanout=meta_fanout
                )
                if live_prefix_digest != stored_prefix_digest:
                    raise SourceTamperError(
                        f"source '{source_name}' content changed within the "
                        f"already-sealed prefix [0, {watermark}) without a "
                        "count change; call build(force_rebuild=True) to "
                        "recover"
                    )

            # Derived summaries are never trusted merely because they parse.
            # Re-derive every retained block from raw source truth before using
            # it for an incremental/no-op build.
            validate_index(root, source_name, events, fanout=fanout)

        if force_rebuild:
            # force_rebuild=True unconditionally resets the per-source cache
            # before reaching this point and reseals from scratch, regardless of which condition
            # triggered the call (shrink, fanout change, or an explicit
            # tamper recovery request). This is also the only path that can
            # recover from count-preserving tamper: SourceTamperError is
            # never raised above when force_rebuild=True, so callers must be
            # able to rely on force_rebuild always re-deriving every block
            # from the live events rather than trusting stale sealed tiers
            # that may no longer match the source.
            watermark = 0
            meta_fanout = fanout

        digest = _source_digest(source_name, fanout)
        sealed_tier1 = 0
        sealed_higher = 0

        # Tier 1: seal every complete fanout-sized block of raw events not
        # yet covered by an existing sealed block.
        #
        # "Already sealed" is determined from `watermark`, not from which
        # tier-1 rows are currently on disk: a range fully inside
        # [0, watermark) was sealed by a previous build() call by
        # definition, even if canonical frontier compaction later removed
        # its on-disk tier-1 row in favor of a coarser sealed parent. If
        # this loop instead re-derived "already sealed" purely from
        # `_read_tier_blocks(..., 1)`, every call after a compaction would
        # see an empty/smaller tier-1 file and wrongly reseal the entire
        # already-covered prefix from scratch on every single build() call
        # — silently duplicating work forever and breaking the no-op
        # no-growth guarantee. Only windows starting at or after
        # `watermark` are candidates; among those, the on-disk check still
        # guards the crash-safe-resume case where a block was appended but
        # the watermark write did not yet happen.
        tier1_blocks = _read_tier_blocks(root, source_name, 1)
        tier1_sealed_ends = {b.end for b in tier1_blocks}
        next_start = (watermark // fanout) * fanout
        block_size = fanout
        while next_start + block_size <= total:
            end = next_start + block_size
            if end not in tier1_sealed_ends:
                window = events[next_start:end]
                texts = [event.text for event in window]
                event_ids_full = [event.event_id for event in window]
                block = Block(
                    tier=1,
                    start=next_start,
                    end=end,
                    summary=_extractive_summary(texts),
                    themes=_derive_themes(texts),
                    event_ids=tuple(event_ids_full[:MAX_ANCHOR_IDS]),
                    schema_version=SCHEMA_VERSION,
                    prompt_version=PROMPT_VERSION,
                    fanout=fanout,
                    source_digest=digest,
                    first_event_id=event_ids_full[0],
                    last_event_id=event_ids_full[-1],
                    raw_sha256=raw_range_digest(event_ids_full, texts),
                )
                _append_block(root, source_name, block)
                sealed_tier1 += 1
                tier1_sealed_ends.add(end)
            next_start = end

        # Higher tiers: roll up from the tier below, one tier at a time,
        # each covering fanout**k raw events per block.
        tier = 2
        block_size = fanout * fanout
        while max_tiers is None or tier <= max_tiers:
            if block_size > max(total, fanout):
                break
            child_blocks = {
                (b.start, b.end): b for b in _read_tier_blocks(root, source_name, tier - 1)
            }
            if not child_blocks:
                break
            existing = {b.end for b in _read_tier_blocks(root, source_name, tier)}
            child_span = block_size // fanout
            block_start = 0
            sealed_any_this_tier = False
            while block_start + block_size <= total:
                block_end = block_start + block_size
                if block_end not in existing:
                    children: list[Block] = []
                    complete = True
                    cursor = block_start
                    while cursor < block_end:
                        child = child_blocks.get((cursor, cursor + child_span))
                        if child is None:
                            complete = False
                            break
                        children.append(child)
                        cursor += child_span
                    if complete and len(children) == fanout:
                        child_summaries = [c.summary for c in children]
                        anchors: list[str] = []
                        for child in children:
                            anchors.extend(child.event_ids)
                        block = Block(
                            tier=tier,
                            start=block_start,
                            end=block_end,
                            summary=_extractive_summary_from_child_summaries(child_summaries),
                            themes=_derive_themes(child_summaries),
                            event_ids=tuple(anchors[:MAX_ANCHOR_IDS]),
                            schema_version=SCHEMA_VERSION,
                            prompt_version=PROMPT_VERSION,
                            fanout=fanout,
                            source_digest=digest,
                            first_event_id=children[0].first_event_id,
                            last_event_id=children[-1].last_event_id,
                            raw_sha256=_child_range_digest(
                                [c.raw_sha256 for c in children]
                            ),
                        )
                        _append_block(root, source_name, block)
                        sealed_higher += 1
                        sealed_any_this_tier = True
                        existing.add(block_end)
                block_start = block_end
            if not sealed_any_this_tier and block_size > total:
                break
            tier += 1
            block_size *= fanout

        highest_tier = max(1, tier - 1)

        no_op = bool(meta) and sealed_tier1 == 0 and sealed_higher == 0 and watermark == total

        if not no_op:
            # Canonical right-frontier compaction: retain uncovered blocks
            # plus all direct children of the rightmost sealed parent at
            # each tier. Those boundary children let assemble() represent
            # the history immediately before a verbatim raw tail without
            # restoring the full O(N) pyramid. The resulting sidecar stays
            # O(fanout * log_fanout(total)), deterministic, and idempotent.
            for compact_tier in range(1, highest_tier):
                _compact_tier(root, source_name, compact_tier)

            sealed_prefix_digest = _recompute_sealed_prefix_digest(
                events, watermark=total, fanout=fanout
            )
            _write_meta(
                root,
                source_name,
                {
                    "watermark": total,
                    "fanout": fanout,
                    "schema_version": SCHEMA_VERSION,
                    "prompt_version": PROMPT_VERSION,
                    "source_digest": digest,
                    "sealed_prefix_digest": sealed_prefix_digest,
                },
            )

        return BuildResult(
            sealed_tier1=sealed_tier1,
            sealed_higher=sealed_higher,
            total_events=total,
            fanout=fanout,
            no_op=no_op,
        )


def _compact_tier(root: Path, source_name: str, tier: int) -> bool:
    """Compact a tier while retaining its right-boundary resolution.

    A tier-k block ``[s, e)`` is covered by a parent iff some sealed
    tier-(k+1) block's range is ``[ps, pe)`` with ``ps <= s`` and
    ``e <= pe`` — with this module's block layout, parent ranges are
    always exact multiples of the child block size and every child of a
    sealed parent is itself sealed (parents are only sealed once all
    ``fanout`` children exist), so in practice a child is covered iff its
    ``end`` does not exceed the highest sealed parent's ``end`` that
    starts at or before the child's start; the containment check above is
    used directly (not assumed) so this stays correct even if that layout
    invariant ever changes. Children of the rightmost parent are retained:
    they form the bounded refinement spine needed to cover a prefix ending
    just before the recent raw tail. Other covered children are removed.

    Rewrites the tier file only when the retained set actually differs
    from what is currently on disk (order-and-content compared), so a
    stable/no-op build performs zero writes here — required to preserve
    the true no-op/no-growth guarantee. Returns True iff a rewrite
    happened.
    """
    current = _read_tier_blocks(root, source_name, tier)
    if not current:
        return False
    parents = _read_tier_blocks(root, source_name, tier + 1)
    if not parents:
        return False

    rightmost_parent = max(parents, key=lambda parent: (parent.end, parent.start))

    def _covered_by_compactable_parent(block: Block) -> bool:
        for parent in parents:
            if parent.start <= block.start and block.end <= parent.end:
                return parent != rightmost_parent
        return False

    kept = [block for block in current if not _covered_by_compactable_parent(block)]
    if len(kept) == len(current):
        return False  # nothing to compact — avoid a no-op rewrite

    kept.sort(key=lambda b: b.start)
    path = _tier_path(root, source_name, tier)
    new_text = "".join(json.dumps(b.to_json(), sort_keys=True) + "\n" for b in kept)
    with private_file_lock(path.with_suffix(".jsonl.lock"), root=root):
        try:
            current_text, _state = read_root_confined_text(
                path,
                root=root,
                max_bytes=100_000_000,
                require_private=True,
                require_owner=True,
                reject_group_other_writable=True,
            )
        except FileNotFoundError:
            current_text = ""
        except (OSError, UnicodeDecodeError) as exc:
            raise IndexIntegrityError(f"unreadable episodic tier {tier}") from exc
        if current_text == new_text:
            return False
        if kept:
            atomic_write_private_text(path, new_text, root=root)
        else:
            atomic_write_private_text(path, "", root=root)
    return True


# ---------------------------------------------------------------------------
# Staircase assembly with honest coverage receipt
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CoverageReceipt:
    """Exact accounting of what a staircase actually covered.

    ``covered`` and ``uncovered`` are lists of half-open ``[start, end)``
    ranges. By construction ``covered + uncovered`` exactly partitions
    ``[0, total)`` with no gaps and no overlaps (asserted in
    ``assemble()``); callers must check ``uncovered`` before assuming the
    staircase represents the whole history.
    """

    total: int
    covered: tuple[tuple[int, int], ...]
    uncovered: tuple[tuple[int, int], ...]
    raw_tail: tuple[int, int]
    stale_blocks_skipped: tuple[tuple[int, int, int], ...]  # (tier, start, end)

    @property
    def fully_covered(self) -> bool:
        """True only if `uncovered` is empty AND `covered` actually tiles
        the whole [0, total) range with no gap. Checking `uncovered` alone
        would be wrong if `covered` simply omitted a region without ever
        recording it as uncovered (a bookkeeping bug, not a legitimate
        state) — this property is the single source of truth callers
        should trust, so it re-derives coverage from `covered` rather than
        only trusting the sibling `uncovered` list.
        """
        if self.uncovered:
            return False
        cursor = 0
        for start, end in sorted(self.covered):
            if start != cursor:
                return False
            cursor = end
        return cursor == self.total

    def to_json(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "covered": [list(pair) for pair in self.covered],
            "uncovered": [list(pair) for pair in self.uncovered],
            "raw_tail": list(self.raw_tail),
            "stale_blocks_skipped": [list(item) for item in self.stale_blocks_skipped],
            "fully_covered": self.fully_covered,
        }


@dataclass(frozen=True)
class StaircaseSegment:
    tier: int  # 0 == raw tail
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class Staircase:
    segments: tuple[StaircaseSegment, ...]
    receipt: CoverageReceipt
    byte_budget: int
    bytes_used: int

    def render(self) -> str:
        lines: list[str] = []
        for segment in self.segments:
            if segment.tier == 0:
                lines.append(f"=== RAW [{segment.start},{segment.end}) ===")
            else:
                lines.append(f"=== T{segment.tier} [{segment.start},{segment.end}) ===")
            lines.append(segment.text)
        return "\n".join(lines)


def _segment_byte_cost(text: str) -> int:
    return len(text.encode("utf-8"))


def assemble(
    root: Path,
    source_name: str,
    events: Sequence[RawEvent],
    *,
    fanout: int = DEFAULT_FANOUT,
    raw_tail: int = 20,
    byte_budget: int = 8_000,
    include_stale: bool = False,
    _index_validated: bool = False,
) -> Staircase:
    """Assemble a coarse-to-fine staircase under a hard byte budget.

    The raw tail (most recent ``raw_tail`` events, verbatim) is reserved
    first — recency is the most valuable budget spend. Everything
    older is covered by the coarsest-available rollup blocks, descending to
    finer tiers only where a coarser block is missing or stale, and finally
    falling back to *no* representation (added to ``uncovered``) rather than
    fabricating coverage. The full verbatim tail is charged before any
    rollup; ``BudgetTooSmallError`` is raised if that requested tail cannot
    fit, so the hard budget is never exceeded. Every other shortfall is
    reported honestly via ``uncovered``, never silently dropped.
    """
    if fanout < 2:
        raise ValueError("fanout must be >= 2")
    _validate_event_sequence(events)
    if not _index_validated:
        validate_index(root, source_name, events, fanout=fanout)
    total = len(events)
    raw_tail = max(0, min(raw_tail, total))
    cut0 = total - raw_tail

    tombstones = read_tombstones(root, source_name)
    blocks_by_tier: dict[int, dict[tuple[int, int], Block]] = {}

    def _tier_blocks(tier: int) -> dict[tuple[int, int], Block]:
        if tier not in blocks_by_tier:
            blocks_by_tier[tier] = {
                (b.start, b.end): b for b in _read_tier_blocks(root, source_name, tier)
            }
        return blocks_by_tier[tier]

    covered: list[tuple[int, int]] = []
    uncovered: list[tuple[int, int]] = []
    stale_skipped: list[tuple[int, int, int]] = []
    segments: list[StaircaseSegment] = []

    tail_events = events[cut0:total] if raw_tail > 0 else ()
    tail_text = "\n".join(event.text for event in tail_events)
    tail_cost = _segment_byte_cost(tail_text) + 32 if raw_tail > 0 else 0
    if tail_cost > byte_budget:
        raise BudgetTooSmallError(
            f"byte_budget={byte_budget} too small for requested raw_tail={raw_tail}"
        )
    remaining_budget = byte_budget - tail_cost

    def _max_tier_for_span(span: int) -> int:
        tier = 1
        while fanout ** (tier + 1) <= span:
            tier += 1
        return tier

    def _cover(start: int, end: int) -> None:
        """Greedily cover [start, end) coarse-to-fine, budget-aware, honest.

        Each sub-range is resolved exactly once: either it becomes a
        rendered segment (``covered``), a budget-rejected span
        (``uncovered``), or a stale-skipped span (``stale_skipped`` AND
        ``uncovered`` — a stale block is never silently re-explored at a
        finer tier, so it cannot be double-reported).
        """
        nonlocal remaining_budget
        if start >= end:
            return
        # A coarser block may extend into the separately rendered raw tail.
        # Prefer that single logarithmic index entry for the historical
        # prefix when it fits: the receipt records the union of represented
        # source ranges, while the overlapping raw segment adds recent
        # detail. This is the intended coarse-to-fine staircase, not duplicate
        # authoritative state.
        if start == 0 and end < total:
            containing_tier = _max_tier_for_span(total) if total >= fanout else 0
            while containing_tier >= 1:
                candidates = sorted(
                    (
                        block
                        for block in _tier_blocks(containing_tier).values()
                        if block.start == 0 and end <= block.end <= total
                    ),
                    key=lambda block: (block.end, block.start),
                )
                for block in candidates:
                    if not include_stale and is_stale(block, tombstones):
                        continue
                    cost = _segment_byte_cost(block.summary) + 32
                    if cost <= remaining_budget:
                        remaining_budget -= cost
                        segments.append(
                            StaircaseSegment(
                                tier=containing_tier,
                                start=block.start,
                                end=block.end,
                                text=block.summary,
                            )
                        )
                        covered.append((block.start, block.end))
                        return
                containing_tier -= 1
        span = end - start
        tier = _max_tier_for_span(span) if span >= fanout else 0
        while tier >= 1:
            block_size = fanout ** tier
            aligned_start = (start // block_size) * block_size
            if aligned_start >= start and aligned_start + block_size <= end:
                block = _tier_blocks(tier).get((aligned_start, aligned_start + block_size))
                if block is not None:
                    if not include_stale and is_stale(block, tombstones):
                        stale_skipped.append((tier, block.start, block.end))
                        uncovered.append((block.start, block.end))
                        if block.start > start:
                            _cover(start, block.start)
                        if block.end < end:
                            _cover(block.end, end)
                        return
                    cost = _segment_byte_cost(block.summary) + 32
                    if cost <= remaining_budget:
                        remaining_budget -= cost
                        segments.append(
                            StaircaseSegment(
                                tier=tier,
                                start=block.start,
                                end=block.end,
                                text=block.summary,
                            )
                        )
                        covered.append((block.start, block.end))
                        if block.start > start:
                            _cover(start, block.start)
                        if block.end < end:
                            _cover(block.end, end)
                        return
                    uncovered.append((start, end))
                    return
            tier -= 1
        # No block available at any tier for this exact aligned window —
        # try descending into finer sub-windows once (tier 1 down to raw
        # is the base case); if still nothing, report honestly.
        if span > fanout:
            mid = start + (span // fanout) * (fanout - 1)
            mid = max(start + 1, min(mid, end - 1))
            _cover(start, mid)
            _cover(mid, end)
            return
        uncovered.append((start, end))

    if cut0 > 0:
        _cover(0, cut0)

    # Sort segments into a stable render order: coarse (high tier) first,
    # each tier ascending by start, mirroring the Headlong staircase.
    segments.sort(key=lambda seg: (-seg.tier, seg.start))

    covered.sort()
    uncovered.sort()
    merged_covered = _merge_ranges(covered)
    merged_uncovered = _merge_ranges(uncovered)

    if raw_tail > 0:
        segments.append(
            StaircaseSegment(tier=0, start=cut0, end=total, text=tail_text)
        )

    bytes_used = sum(_segment_byte_cost(seg.text) + 32 for seg in segments)

    # The raw tail, when present, is always rendered verbatim in full (see
    # above: its budget is reserved before coarser segments) — so whenever
    # it is present in `segments` it is
    # genuinely covered and belongs in `covered`, not left implicit in the
    # separate `raw_tail` field. Folding it in here means `fully_covered`
    # (covered vs. [0, total)) is correct on its own, without a caller having
    # to additionally cross-check `raw_tail` — a receipt should never require
    # reading two fields to know if a region is covered.
    tail_rendered = any(seg.tier == 0 for seg in segments)
    if tail_rendered and raw_tail > 0:
        covered.append((cut0, total))
        covered.sort()
        merged_covered = _merge_ranges(covered)

    receipt = CoverageReceipt(
        total=total,
        covered=tuple(merged_covered),
        uncovered=tuple(merged_uncovered),
        raw_tail=(cut0, total),
        stale_blocks_skipped=tuple(stale_skipped),
    )
    return Staircase(
        segments=tuple(segments),
        receipt=receipt,
        byte_budget=byte_budget,
        bytes_used=bytes_used,
    )


def _merge_ranges(ranges: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    if not ranges:
        return []
    ordered = sorted(ranges)
    merged = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [tuple(pair) for pair in merged]


# ---------------------------------------------------------------------------
# Drill-down / citation resolution
# ---------------------------------------------------------------------------


def drill_down(
    events: Sequence[RawEvent],
    *,
    event_id: str | None = None,
    range_: tuple[int, int] | None = None,
) -> list[RawEvent]:
    """Resolve a rollup anchor back to raw events.

    Exactly one of ``event_id`` or ``range_`` must be given. Returns an
    empty list (never raises) when nothing matches, so a stale anchor from
    an old block is a visible empty result rather than a crash.
    """
    if (event_id is None) == (range_ is None):
        raise ValueError("drill_down requires exactly one of event_id or range_")
    if event_id is not None:
        return [event for event in events if event.event_id == event_id]
    start, end = range_  # type: ignore[misc]
    return [event for event in events if start <= event.index < end]


def resolve_citations(events: Sequence[RawEvent], block: Block) -> list[RawEvent]:
    """Resolve a block's anchor ``event_ids`` back to raw event rows.

    Ids that no longer resolve (e.g. after a defensive rebuild changed
    fallback hashing inputs) are simply omitted rather than raising, so a
    partially-resolvable citation set is still usable.
    """
    index = {event.event_id: event for event in events}
    return [index[event_id] for event_id in block.event_ids if event_id in index]


__all__ = [
    "SCHEMA_VERSION",
    "PROMPT_VERSION",
    "DEFAULT_FANOUT",
    "DEFAULT_SUMMARY_CHARS",
    "MAX_ANCHOR_IDS",
    "EpisodicMemoryError",
    "SourceShrinkError",
    "SourceTamperError",
    "IndexIntegrityError",
    "BudgetTooSmallError",
    "RawEvent",
    "stable_event_id",
    "load_jsonl_events",
    "raw_range_digest",
    "Block",
    "episodic_dir",
    "Tombstone",
    "read_tombstones",
    "tombstone_range",
    "is_stale",
    "BuildResult",
    "build",
    "validate_index",
    "CoverageReceipt",
    "StaircaseSegment",
    "Staircase",
    "assemble",
    "drill_down",
    "resolve_citations",
]
