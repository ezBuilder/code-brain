"""Code Brain integration for the deterministic episodic-memory pyramid.

Raw audit JSONL remains the source of truth.  The pyramid and its compact hook
cache are disposable indexes: explicit reads always carry a coverage receipt,
and important decisions must drill back to raw events.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

from . import episodic_memory as pyramid
from .memory import _audit_file_sort_key, all_audit_files, audit_segment_sequence_issues
from .private_write import (
    atomic_write_private_text,
    list_root_confined_directory,
    read_root_confined_text,
    validate_root_confined_regular_file,
)

AUDIT_SOURCE_NAME = "audit"
DEFAULT_CONTEXT_BYTE_BUDGET = 8_000
DEFAULT_CONTEXT_RAW_TAIL = 20
HOOK_CONTEXT_MAX_BYTES = 200
MAX_AUDIT_FILE_BYTES = 100_000_000
MAX_AUDIT_EVENTS = 2_000_000
MAX_DRILLDOWN_EVENTS = 200
_HOOK_CACHE_SCHEMA = 1
_HISTORY_GAP_SCHEMA = 1


class EpisodicRuntimeError(RuntimeError):
    """Raised when the raw audit corpus cannot be indexed safely."""


@dataclass(frozen=True)
class AuditCorpus:
    events: tuple[pyramid.RawEvent, ...]
    source_states: tuple[dict[str, Any], ...]
    malformed_rows: int
    legacy_id_rows: int
    legacy_fold_rows: int
    raw_bytes: int
    digest: str


def _clip_utf8(text: str, max_bytes: int) -> str:
    budget = max(0, int(max_bytes))
    encoded = text.encode("utf-8")
    if len(encoded) <= budget:
        return text
    if budget <= 3:
        return "." * budget
    clipped = encoded[: budget - 3]
    while clipped:
        try:
            return clipped.decode("utf-8").rstrip() + "..."
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return "..."[:budget]


def _compact_json(value: Any, *, max_chars: int = 360) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        text = str(value)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= max_chars else text[: max(0, max_chars - 1)].rstrip() + "…"


def _event_text(record: dict[str, Any]) -> str:
    action = str(record.get("action") or "unknown")[:96]
    ts = str(record.get("ts") or "")[:19]
    payload = record.get("payload")
    if action == "_folded":
        date = payload.get("date") if isinstance(payload, dict) else ""
        return (
            f"{ts} legacy-lossy-fold date={date}; original raw events unavailable; "
            "exclude from complete coverage"
        ).strip()
    category = str(record.get("category") or "")[:48]
    bits = [part for part in (ts, action, f"category={category}" if category else "") if part]
    if payload not in (None, {}, []):
        bits.append(_compact_json(payload))
    return " ".join(bits)


def load_audit_corpus(root: Path) -> AuditCorpus:
    """Load trusted audit files into one physical-order event sequence.

    Per-file line numbers remain attached as provenance while ``RawEvent.index``
    is a global contiguous ordinal, matching the pyramid range contract.
    """

    root = Path(root)
    events: list[pyramid.RawEvent] = []
    source_states: list[dict[str, Any]] = []
    malformed = 0
    legacy_ids = 0
    legacy_folds = 0
    raw_bytes = 0
    corpus_hash = hashlib.sha256()
    audit_directory = root / ".ai" / "memory" / "audit"
    try:
        audit_names = list_root_confined_directory(
            audit_directory,
            root=root,
            max_entries=4_096,
        )
    except FileNotFoundError:
        audit_names = []
    except OSError as exc:
        raise EpisodicRuntimeError(f"untrusted audit source directory: {type(exc).__name__}") from exc
    audit_files = all_audit_files(root)
    canonical_names = sorted(
        (name for name in audit_names if _audit_file_sort_key(name) is not None),
        key=lambda name: _audit_file_sort_key(name) or (0, 0, 0),
    )
    if [path.name for path in audit_files] != canonical_names:
        raise EpisodicRuntimeError("untrusted or unreadable audit source discovered")
    sequence_issues = audit_segment_sequence_issues(audit_files)
    if sequence_issues:
        issue = sequence_issues[0]
        if issue.get("kind") == "duplicate":
            raise EpisodicRuntimeError("duplicate audit segment sequence")
        raise EpisodicRuntimeError(
            f"audit segment sequence {issue.get('kind')} for year {issue.get('year')}"
        )
    max_segment_by_year: dict[int, int] = {}
    segment_sequences: set[tuple[int, int]] = set()
    for path in audit_files:
        key = _audit_file_sort_key(path.name)
        if key is None:
            raise EpisodicRuntimeError("non-canonical audit source discovered")
        if key[1] != 0:
            continue
        identity = (key[0], key[2])
        if identity in segment_sequences:
            raise EpisodicRuntimeError("duplicate audit segment sequence")
        segment_sequences.add(identity)
        max_segment_by_year[key[0]] = max(max_segment_by_year.get(key[0], 0), key[2])

    seen_event_ids: set[str] = set()
    seen_file_digests: set[tuple[int, str]] = set()
    previous_year: int | None = None
    previous_rel: str | None = None
    previous_last_sha: str | None = None
    previous_file_sha256: str | None = None
    previous_file_bytes: int | None = None

    for path in audit_files:
        rel = path.relative_to(root).as_posix()
        sort_key = _audit_file_sort_key(path.name)
        if sort_key is None:  # guarded above; keep the invariant local for type checkers
            raise EpisodicRuntimeError("non-canonical audit source discovered")
        year, kind, sequence = sort_key
        logical_sequence = sequence if kind == 0 else max_segment_by_year.get(year, 0) + 1
        legacy_namespace = f"audit:{year}:{logical_sequence:06d}"
        try:
            text, state = read_root_confined_text(
                path,
                root=root,
                max_bytes=MAX_AUDIT_FILE_BYTES,
                require_private=False,
                require_owner=True,
                reject_group_other_writable=True,
            )
        except (OSError, UnicodeDecodeError) as exc:
            raise EpisodicRuntimeError(f"unreadable audit source: {rel}: {type(exc).__name__}") from exc
        file_bytes = len(text.encode("utf-8"))
        file_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if file_bytes != int(state.st_size):
            raise EpisodicRuntimeError(f"audit source size changed while reading: {rel}")
        if kind == 0 and path.name.split(".")[2] != file_sha256[:12]:
            raise EpisodicRuntimeError(f"audit segment digest mismatch: {rel}")
        file_identity = (year, file_sha256)
        if file_bytes and file_identity in seen_file_digests:
            raise EpisodicRuntimeError(f"duplicate audit source content: {rel}")
        seen_file_digests.add(file_identity)

        nonempty_lines = [line for line in text.splitlines() if line.strip()]
        try:
            first_record = json.loads(nonempty_lines[0]) if nonempty_lines else None
        except (json.JSONDecodeError, ValueError):
            first_record = None
        payload = (
            first_record.get("payload")
            if isinstance(first_record, dict) and isinstance(first_record.get("payload"), dict)
            else {}
        )
        if previous_year is None or year != previous_year:
            if isinstance(first_record, dict) and first_record.get("action") == "audit.segment_started":
                raise EpisodicRuntimeError(f"orphan audit segment marker: {rel}")
        else:
            if not isinstance(first_record, dict) or first_record.get("action") != "audit.segment_started":
                raise EpisodicRuntimeError(f"audit segment link marker missing: {rel}")
            if (
                payload.get("previous_segment") != previous_rel
                or payload.get("previous_last_sha") != previous_last_sha
                or payload.get("previous_file_sha256") != previous_file_sha256
                or payload.get("bytes_segmented") != previous_file_bytes
                or payload.get("lossy") is not False
            ):
                raise EpisodicRuntimeError(f"audit segment link mismatch: {rel}")

        raw_bytes += file_bytes
        source_states.append(
            {
                "path": rel,
                "size": int(state.st_size),
                "mtime_ns": int(state.st_mtime_ns),
                "device": int(state.st_dev),
                "inode": int(state.st_ino),
            }
        )
        previous_line: str | None = None
        for source_line, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, ValueError) as exc:
                raise EpisodicRuntimeError(f"malformed audit row: {rel}:{source_line}") from exc
            if not isinstance(record, dict):
                raise EpisodicRuntimeError(f"non-object audit row: {rel}:{source_line}")
            if "event_id" in record:
                source_event_id = record.get("event_id")
                if (
                    not isinstance(source_event_id, str)
                    or not source_event_id.startswith("evt-")
                    or len(source_event_id) != 36
                ):
                    raise EpisodicRuntimeError(f"invalid audit event id: {rel}:{source_line}")
            if "prev_sha" in record:
                previous_sha = record.get("prev_sha")
                if previous_sha is not None and (
                    not isinstance(previous_sha, str) or len(previous_sha) != 64
                ):
                    raise EpisodicRuntimeError(f"invalid audit prev_sha: {rel}:{source_line}")
                expected_previous = (
                    hashlib.sha256(previous_line.encode("utf-8")).hexdigest()
                    if previous_line is not None
                    else None
                )
                if previous_sha != expected_previous:
                    raise EpisodicRuntimeError(f"audit hash-chain mismatch: {rel}:{source_line}")
            if len(events) >= MAX_AUDIT_EVENTS:
                raise EpisodicRuntimeError(
                    f"audit event limit exceeded ({MAX_AUDIT_EVENTS}); refusing partial coverage"
                )
            if record.get("action") == "_folded":
                legacy_folds += 1
            if not isinstance(record.get("event_id"), str) or not str(record.get("event_id") or "").strip():
                legacy_ids += 1
            index = len(events)
            event_id = pyramid.stable_event_id(
                index,
                record,
                namespace=legacy_namespace,
                source_line=source_line,
            )
            if event_id in seen_event_ids:
                raise EpisodicRuntimeError(f"duplicate audit event id: {rel}:{source_line}")
            seen_event_ids.add(event_id)
            raw = dict(record)
            raw["_cb_source_path"] = rel
            raw["_cb_source_line"] = source_line
            event = pyramid.RawEvent(
                index=index,
                event_id=event_id,
                text=_event_text(record),
                raw=raw,
                source_line=source_line,
            )
            events.append(event)
            corpus_hash.update(rel.encode("utf-8"))
            corpus_hash.update(b"\0")
            corpus_hash.update(str(source_line).encode("ascii"))
            corpus_hash.update(b"\0")
            corpus_hash.update(event_id.encode("utf-8"))
            corpus_hash.update(b"\0")
            corpus_hash.update(event.text.encode("utf-8"))
            corpus_hash.update(b"\n")
            previous_line = line

        previous_year = year
        previous_rel = rel
        previous_last_sha = (
            hashlib.sha256(nonempty_lines[-1].encode("utf-8")).hexdigest()
            if nonempty_lines
            else None
        )
        previous_file_sha256 = file_sha256
        previous_file_bytes = file_bytes

    if all_audit_files(root) != audit_files:
        raise EpisodicRuntimeError("audit source set changed while reading")
    for item in source_states:
        source = root / str(item["path"])
        try:
            state = validate_root_confined_regular_file(
                source,
                root=root,
                require_owner=True,
                reject_group_other_writable=True,
            )
        except (FileNotFoundError, OSError) as exc:
            raise EpisodicRuntimeError("audit source changed while reading") from exc
        if (
            int(state.st_size) != int(item["size"])
            or int(state.st_mtime_ns) != int(item["mtime_ns"])
            or int(state.st_dev) != int(item["device"])
            or int(state.st_ino) != int(item["inode"])
        ):
            raise EpisodicRuntimeError("audit source changed while reading")

    return AuditCorpus(
        events=tuple(events),
        source_states=tuple(source_states),
        malformed_rows=malformed,
        legacy_id_rows=legacy_ids,
        legacy_fold_rows=legacy_folds,
        raw_bytes=raw_bytes,
        digest=corpus_hash.hexdigest(),
    )


def _hook_cache_path(root: Path) -> Path:
    return pyramid.episodic_dir(root, AUDIT_SOURCE_NAME) / "hook-context.json"


def _history_gap_path(root: Path) -> Path:
    return pyramid.episodic_dir(root, AUDIT_SOURCE_NAME) / "history-gap.json"


def _read_history_gap(root: Path) -> dict[str, Any] | None:
    path = _history_gap_path(root)
    try:
        text, _state = read_root_confined_text(path, root=root, max_bytes=8_192)
        loaded = json.loads(text)
        previous = int(loaded.get("previous_indexed_events", -1))
        current = int(loaded.get("current_raw_events", -1))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if (
        not isinstance(loaded, dict)
        or loaded.get("schema_version") != _HISTORY_GAP_SCHEMA
        or loaded.get("reason") != "source_shrink"
        or previous < 0
        or current < 0
        or previous <= current
    ):
        return None
    return {
        "schema_version": _HISTORY_GAP_SCHEMA,
        "reason": "source_shrink",
        "previous_indexed_events": previous,
        "current_raw_events": current,
    }


def _index_watermark(root: Path) -> int:
    path = pyramid.episodic_dir(root, AUDIT_SOURCE_NAME) / "meta.json"
    try:
        text, _state = read_root_confined_text(path, root=root, max_bytes=65_536)
        loaded = json.loads(text)
        return max(0, int(loaded.get("watermark", 0) or 0)) if isinstance(loaded, dict) else 0
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return 0


def _write_if_changed(path: Path, text: str, *, root: Path) -> bool:
    try:
        previous, _state = read_root_confined_text(path, root=root, max_bytes=16_384)
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        previous = None
    if previous == text:
        return False
    atomic_write_private_text(path, text, root=root)
    return True


def _build_hook_line(
    staircase: pyramid.Staircase,
    *,
    legacy_fold_rows: int,
    history_gap: dict[str, Any] | None,
) -> str:
    segments = [segment for segment in staircase.segments if segment.tier > 0]
    if not segments:
        return ""
    prefix = "cb-life: index only; raw audit is truth; drill down before decisions."
    if legacy_fold_rows:
        prefix += f" legacy-lossy={legacy_fold_rows}."
    if history_gap:
        prefix += (
            " history-gap="
            f"{history_gap['previous_indexed_events']}->{history_gap['current_raw_events']}."
        )
    details: list[str] = []
    for segment in segments[:3]:
        summary = re.sub(r"\s+", " ", segment.text).strip()
        if summary:
            details.append(f"T{segment.tier}[{segment.start},{segment.end}) {summary}")
    line = prefix + (" " + " | ".join(details) if details else "")
    return _clip_utf8(line, HOOK_CONTEXT_MAX_BYTES)


def build_audit_index(
    root: Path,
    *,
    fanout: int = pyramid.DEFAULT_FANOUT,
    force_rebuild: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Build the audit pyramid offline; never call this from a hook read path."""

    root = Path(root)
    fanout = int(fanout)
    if not 2 <= fanout <= 100:
        return {"ok": False, "reason": "invalid_fanout"}
    corpus = load_audit_corpus(root)
    history_gap = _read_history_gap(root)
    meta_path = pyramid.episodic_dir(root, AUDIT_SOURCE_NAME) / "meta.json"
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "would_index_events": len(corpus.events),
            "fanout": fanout,
            "legacy_fold_rows": corpus.legacy_fold_rows,
            "malformed_rows": corpus.malformed_rows,
            "source_history_gap": history_gap,
            "source_truth_complete": (
                corpus.legacy_fold_rows == 0
                and corpus.malformed_rows == 0
                and history_gap is None
            ),
        }
    if not corpus.events and not meta_path.exists():
        return {
            "ok": True,
            "dry_run": False,
            "built": False,
            "reason": "no_audit_events",
            "legacy_fold_rows": corpus.legacy_fold_rows,
            "malformed_rows": 0,
            "source_history_gap": history_gap,
            "source_truth_complete": history_gap is None,
        }

    repaired = False
    repair_reason: str | None = None
    previous_watermark = _index_watermark(root)
    try:
        result = pyramid.build(
            root,
            AUDIT_SOURCE_NAME,
            corpus.events,
            fanout=fanout,
            force_rebuild=force_rebuild,
        )
    except pyramid.SourceShrinkError:
        if force_rebuild:
            raise
        prior = max(previous_watermark, int((history_gap or {}).get("previous_indexed_events", 0)))
        history_gap = {
            "schema_version": _HISTORY_GAP_SCHEMA,
            "reason": "source_shrink",
            "previous_indexed_events": prior,
            "current_raw_events": len(corpus.events),
        }
        result = pyramid.build(
            root,
            AUDIT_SOURCE_NAME,
            corpus.events,
            fanout=fanout,
            force_rebuild=True,
        )
        repaired = True
        repair_reason = "source_shrink"
    except pyramid.IndexIntegrityError:
        if force_rebuild:
            raise
        result = pyramid.build(
            root,
            AUDIT_SOURCE_NAME,
            corpus.events,
            fanout=fanout,
            force_rebuild=True,
        )
        repaired = True
        repair_reason = "derived_index_integrity"
    if history_gap:
        _write_if_changed(
            _history_gap_path(root),
            json.dumps(history_gap, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            root=root,
        )
    staircase = pyramid.assemble(
        root,
        AUDIT_SOURCE_NAME,
        corpus.events,
        fanout=fanout,
        raw_tail=0,
        byte_budget=1_024,
    )
    hook_line = _build_hook_line(
        staircase,
        legacy_fold_rows=corpus.legacy_fold_rows,
        history_gap=history_gap,
    )
    cache_payload = {
        "schema_version": _HOOK_CACHE_SCHEMA,
        "authoritative": False,
        "source_of_truth": ".ai/memory/audit/*.jsonl",
        "source_digest": corpus.digest,
        "source_states": list(corpus.source_states),
        "indexed_events": len(corpus.events),
        "legacy_fold_rows": corpus.legacy_fold_rows,
        "source_history_gap": history_gap,
        "text": hook_line,
    }
    canonical_payload = json.dumps(
        cache_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    cache_payload["payload_sha256"] = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
    cache_text = json.dumps(
        cache_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
    cache_changed = _write_if_changed(_hook_cache_path(root), cache_text, root=root)
    return {
        "ok": True,
        "dry_run": False,
        "build": asdict(result),
        "repaired": repaired,
        "repair_reason": repair_reason,
        "cache_changed": cache_changed,
        "hook_bytes": len(hook_line.encode("utf-8")),
        "receipt": staircase.receipt.to_json(),
        "legacy_fold_rows": corpus.legacy_fold_rows,
        "malformed_rows": corpus.malformed_rows,
        "source_history_gap": history_gap,
        "source_truth_complete": (
            corpus.legacy_fold_rows == 0
            and corpus.malformed_rows == 0
            and history_gap is None
        ),
    }


def read_hook_context(root: Path) -> str:
    """Read only the tiny prebuilt cache; no pyramid build or raw scan."""

    root = Path(root)
    path = _hook_cache_path(root)
    try:
        text, _state = read_root_confined_text(path, root=root, max_bytes=16_384)
        payload = json.loads(text)
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return ""
    if not isinstance(payload, dict) or payload.get("schema_version") != _HOOK_CACHE_SCHEMA:
        return ""
    expected_payload_sha256 = payload.pop("payload_sha256", None)
    canonical_payload = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if (
        not isinstance(expected_payload_sha256, str)
        or hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest() != expected_payload_sha256
    ):
        return ""
    states = payload.get("source_states")
    if not isinstance(states, list):
        return ""
    cached_paths = [str(item.get("path")) for item in states if isinstance(item, dict)]
    live_paths = [path.relative_to(root).as_posix() for path in all_audit_files(root)]
    if cached_paths != live_paths:
        return ""
    append_stale = False
    for item in states:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            return ""
        source = root / str(item["path"])
        try:
            current = validate_root_confined_regular_file(
                source,
                root=root,
                require_owner=True,
                reject_group_other_writable=True,
            )
        except (FileNotFoundError, OSError):
            return ""
        cached_size = int(item.get("size", -1))
        cached_mtime = int(item.get("mtime_ns", -1))
        cached_device = int(item.get("device", -1))
        cached_inode = int(item.get("inode", -1))
        if cached_device < 0 or cached_inode < 0:
            return ""
        if int(current.st_dev) != cached_device or int(current.st_ino) != cached_inode:
            return ""
        if int(current.st_size) < cached_size:
            return ""
        if int(current.st_size) > cached_size:
            append_stale = True
        elif cached_mtime >= 0 and int(current.st_mtime_ns) != cached_mtime:
            return ""
    if append_stale:
        return "cb-life: episodic index stale; raw audit advanced; rebuild before memory use."
    line = str(payload.get("text") or "")
    if not line.startswith("cb-life:"):
        return ""
    return _clip_utf8(line, HOOK_CONTEXT_MAX_BYTES)


def context_payload(
    root: Path,
    *,
    byte_budget: int = DEFAULT_CONTEXT_BYTE_BUDGET,
    raw_tail: int = DEFAULT_CONTEXT_RAW_TAIL,
    fanout: int = pyramid.DEFAULT_FANOUT,
) -> dict[str, Any]:
    """Assemble an explicit progressive-resolution view with an honest receipt."""

    root = Path(root)
    history_gap = _read_history_gap(root)
    budget = max(256, min(int(byte_budget), 64_000))
    fanout = int(fanout)
    if not 2 <= fanout <= 100:
        return {"ok": False, "reason": "invalid_fanout"}
    bounded_raw_tail = max(0, min(int(raw_tail), 200))
    corpus = load_audit_corpus(root)
    if not corpus.events:
        return {
            "ok": False,
            "reason": "no_audit_events",
            "authoritative": False,
            "source_of_truth": ".ai/memory/audit/*.jsonl",
        }
    header = (
        "EPISODIC INDEX — NON-AUTHORITATIVE. Raw audit is the source of truth; "
        "drill down before important decisions.\n"
    )
    available = budget - len(header.encode("utf-8"))
    try:
        staircase = pyramid.assemble(
            root,
            AUDIT_SOURCE_NAME,
            corpus.events,
            fanout=fanout,
            raw_tail=bounded_raw_tail,
            byte_budget=available,
        )
    except pyramid.BudgetTooSmallError as exc:
        return {"ok": False, "reason": "budget_too_small", "detail": str(exc)}
    except pyramid.IndexIntegrityError:
        return {"ok": False, "reason": "invalid_index", "detail": "rebuild episodic index"}
    rendered = header + staircase.render()
    used = len(rendered.encode("utf-8"))
    if used > budget:
        return {
            "ok": False,
            "reason": "budget_accounting_error",
            "byte_budget": budget,
            "bytes_used": used,
        }
    return {
        "ok": True,
        "authoritative": False,
        "source_of_truth": ".ai/memory/audit/*.jsonl",
        "drilldown_required": True,
        "text": rendered,
        "byte_budget": budget,
        "bytes_used": used,
        "receipt": staircase.receipt.to_json(),
        "legacy_fold_rows": corpus.legacy_fold_rows,
        "malformed_rows": corpus.malformed_rows,
        "source_history_gap": history_gap,
        "source_truth_complete": (
            corpus.legacy_fold_rows == 0
            and corpus.malformed_rows == 0
            and history_gap is None
        ),
    }


def drilldown_payload(
    root: Path,
    *,
    event_id: str | None = None,
    start: int | None = None,
    end: int | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Resolve an event id or half-open global range back to raw audit rows."""

    has_id = bool(event_id)
    has_range = start is not None or end is not None
    if has_id == has_range:
        return {"ok": False, "reason": "provide_event_id_or_range"}
    if has_range and (start is None or end is None or start < 0 or end <= start):
        return {"ok": False, "reason": "invalid_range"}
    corpus = load_audit_corpus(Path(root))
    if has_id:
        matches = pyramid.drill_down(corpus.events, event_id=str(event_id))
    else:
        matches = pyramid.drill_down(corpus.events, range_=(int(start), int(end)))
    bounded_limit = max(1, min(int(limit), MAX_DRILLDOWN_EVENTS))
    records: list[dict[str, Any]] = []
    for event in matches[:bounded_limit]:
        raw = dict(event.raw)
        source_path = str(raw.pop("_cb_source_path", ""))
        source_line = int(raw.pop("_cb_source_line", event.source_line))
        records.append(
            {
                "index": event.index,
                "event_id": event.event_id,
                "source": {"path": source_path, "line": source_line},
                "record": raw,
            }
        )
    return {
        "ok": bool(matches),
        "reason": None if matches else "no_match",
        "count": len(records),
        "matched": len(matches),
        "truncated": len(matches) > len(records),
        "events": records,
        "authoritative": True,
    }


def status(root: Path) -> dict[str, Any]:
    """Report source/index health without writing or rebuilding."""

    root = Path(root)
    history_gap = _read_history_gap(root)
    try:
        corpus = load_audit_corpus(root)
    except EpisodicRuntimeError as exc:
        return {
            "ok": False,
            "ready": False,
            "integrity_ok": False,
            "reason": str(exc),
        }
    directory = pyramid.episodic_dir(root, AUDIT_SOURCE_NAME)
    meta_path = directory / "meta.json"
    meta: dict[str, Any] = {}
    if meta_path.exists():
        try:
            text, _state = read_root_confined_text(meta_path, root=root, max_bytes=65_536)
            loaded = json.loads(text)
            if isinstance(loaded, dict):
                meta = loaded
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return {"ok": False, "reason": "invalid_meta"}
    tier_files = sorted(directory.glob("tier_*.jsonl")) if directory.is_dir() else []
    index_bytes = 0
    tier_rows = 0
    invalid_rows = 0
    for path in tier_files:
        try:
            text, state = read_root_confined_text(path, root=root, max_bytes=100_000_000)
        except (OSError, UnicodeDecodeError):
            invalid_rows += 1
            continue
        index_bytes += int(state.st_size)
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                loaded = json.loads(line)
                pyramid.Block.from_json(loaded)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                invalid_rows += 1
                continue
            tier_rows += 1
    watermark = int(meta.get("watermark", 0) or 0)
    built = bool(meta)
    stale = built and watermark != len(corpus.events)
    integrity_ok = True
    if built:
        fanout = int(meta.get("fanout", pyramid.DEFAULT_FANOUT) or pyramid.DEFAULT_FANOUT)
        stored_prefix = str(meta.get("sealed_prefix_digest", "") or "")
        if watermark < 0 or watermark > len(corpus.events) or not stored_prefix:
            integrity_ok = False
        else:
            try:
                live_prefix = pyramid._recompute_sealed_prefix_digest(
                    corpus.events, watermark=watermark, fanout=fanout
                )
            except (TypeError, ValueError):
                integrity_ok = False
            else:
                integrity_ok = live_prefix == stored_prefix
        if integrity_ok:
            try:
                validated = pyramid.validate_index(
                    root,
                    AUDIT_SOURCE_NAME,
                    corpus.events,
                    fanout=fanout,
                )
            except (pyramid.EpisodicMemoryError, TypeError, ValueError, OverflowError):
                integrity_ok = False
                invalid_rows += 1
            else:
                tier_rows = int(validated.get("tier_rows", tier_rows))
                index_bytes = int(validated.get("index_bytes", index_bytes))
    return {
        "ok": invalid_rows == 0 and integrity_ok,
        "built": built,
        "ready": built and not stale and invalid_rows == 0 and integrity_ok,
        "stale": stale,
        "integrity_ok": integrity_ok,
        "raw_events": len(corpus.events),
        "raw_bytes": corpus.raw_bytes,
        "raw_files": len(corpus.source_states),
        "indexed_events": watermark,
        "fanout": int(meta.get("fanout", pyramid.DEFAULT_FANOUT) or pyramid.DEFAULT_FANOUT),
        "tier_files": len(tier_files),
        "tier_rows": tier_rows,
        "index_bytes": index_bytes,
        "invalid_rows": invalid_rows,
        "malformed_rows": corpus.malformed_rows,
        "legacy_id_rows": corpus.legacy_id_rows,
        "legacy_fold_rows": corpus.legacy_fold_rows,
        "source_history_gap": history_gap,
        "source_truth_complete": (
            corpus.legacy_fold_rows == 0
            and corpus.malformed_rows == 0
            and history_gap is None
        ),
    }


__all__: Sequence[str] = (
    "AUDIT_SOURCE_NAME",
    "DEFAULT_CONTEXT_BYTE_BUDGET",
    "DEFAULT_CONTEXT_RAW_TAIL",
    "HOOK_CONTEXT_MAX_BYTES",
    "EpisodicRuntimeError",
    "AuditCorpus",
    "load_audit_corpus",
    "build_audit_index",
    "read_hook_context",
    "context_payload",
    "drilldown_payload",
    "status",
)
