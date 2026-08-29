from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections import deque
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .private_write import (
    append_private_text,
    atomic_write_private_text,
    iter_root_confined_text_lines,
    list_root_confined_directory,
    private_file_lock,
    read_root_confined_tail_bytes,
    read_root_confined_text,
    unlink_root_confined_regular_file,
    validate_root_confined_regular_file,
)
from .redact import redact_value

_AUDIT_THREAD_LOCK = threading.RLock()
_AUDIT_FILE_MAX_COUNT = 4_096
_AUDIT_LINE_MAX_BYTES = 1_000_000
_AUDIT_SEGMENT_MARKER_MAX_BYTES = 4_096
_AUDIT_ACTION_MAX_CHARS = 256
_AUDIT_CATEGORY_MAX_CHARS = 128
_JSONL_TAIL_MAX_LIMIT = 1_000
_JSONL_TAIL_MIN_BYTES = 256 * 1024
_JSONL_TAIL_MAX_BYTES = 8 * 1024 * 1024
_JSONL_TAIL_BYTES_PER_ITEM = 64 * 1024
_JSONL_LINE_MAX_BYTES = 1_000_000
_JSONL_ROTATE_MAX_BYTES = 100_000_000
_JSONL_ROTATE_MAX_LINES = 100_000
_JSONL_AUTO_MAX_BYTES = 32 * 1024 * 1024
_JSONL_AUTO_KEEP_BYTES = 16 * 1024 * 1024
_JSONL_AUTO_KEEP_LINES = 50_000
_AUDIT_MAX_BYTES = 64 * 1024 * 1024
_AUDIT_RETENTION_YEARS = 3
_AUDIT_INDEX_MAX_ROWS = 50_000
_TEXT_TAIL_MAX_LINES = 1_000
_TEXT_TAIL_MIN_BYTES = 64 * 1024
_TEXT_TAIL_MAX_BYTES = 8 * 1024 * 1024
_TEXT_TAIL_BYTES_PER_LINE = 64 * 1024
_JSONL_ALL_MAX_BYTES = 100_000_000
_JSONL_ALL_MAX_RECORDS = 100_000
_OPEN_TODO_MAX_LIMIT = 1_000
_ROTATION_NOTICE_SUFFIX = ".rotation.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def line_sha(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def _new_audit_event_id() -> str:
    """Return an opaque ID that remains stable after serialization and rotation."""
    return f"evt-{uuid.uuid4().hex}"


def _previous_audit_sha(path: Path, *, root: Path) -> str | None:
    line_limit = max(_AUDIT_LINE_MAX_BYTES, _AUDIT_SEGMENT_MARKER_MAX_BYTES)
    try:
        data, _state, complete = read_root_confined_tail_bytes(
            path,
            root=root,
            max_bytes=line_limit + 1,
            require_private=False,
            require_owner=True,
            reject_group_other_writable=True,
        )
    except FileNotFoundError:
        return None
    if not data:
        return None
    if not complete:
        boundary = data.find(b"\n")
        if boundary < 0:
            raise OSError("previous audit record exceeds line limit")
        data = data[boundary + 1:]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OSError("audit tail is not valid UTF-8") from exc
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        if complete:
            return None
        raise OSError("previous audit record exceeds line limit")
    last = lines[-1]
    last_bytes = len(last.encode("utf-8"))
    if last_bytes > line_limit:
        raise OSError("previous audit record exceeds line limit")
    if last_bytes > _AUDIT_LINE_MAX_BYTES:
        try:
            marker = json.loads(last)
        except (json.JSONDecodeError, TypeError) as exc:
            raise OSError("previous audit record exceeds line limit") from exc
        if not isinstance(marker, dict) or marker.get("action") != "audit.segment_started":
            raise OSError("previous audit record exceeds line limit")
    return line_sha(last)


def _bounded_audit_line(
    *,
    timestamp: datetime,
    action: object,
    category: object,
    payload: object,
    prev_sha: str | None,
    event_id: str | None = None,
    max_bytes: int | None = None,
) -> tuple[dict[str, Any], str]:
    line_limit = _AUDIT_LINE_MAX_BYTES if max_bytes is None else max(1, int(max_bytes))
    action_clean = str(redact_value(action))[:_AUDIT_ACTION_MAX_CHARS]
    category_clean = str(redact_value(category))[:_AUDIT_CATEGORY_MAX_CHARS]
    payload_clean = redact_value(payload)
    record: dict[str, Any] = {
        "ts": timestamp.isoformat().replace("+00:00", "Z"),
        "monotonic_ns": time.monotonic_ns(),
        "action": action_clean,
        "category": category_clean,
        "payload": payload_clean,
        "prev_sha": prev_sha,
    }
    if event_id is not None:
        record["event_id"] = event_id
    line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    encoded = line.encode("utf-8")
    if len(encoded) <= line_limit:
        return record, line
    payload_bytes = json.dumps(
        payload_clean,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    record["payload"] = {
        "_truncated": True,
        "bytes": len(payload_bytes),
        "sha256": hashlib.sha256(payload_bytes).hexdigest(),
    }
    line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(line.encode("utf-8")) > line_limit:
        raise OSError("audit record exceeds line limit")
    return record, line


def state_root_for_path(path: Path) -> Path:
    """Infer the project root for a lexical ``<root>/.ai/...`` state path."""
    path = Path(path)
    for parent in (path.parent, *path.parents):
        if parent.name == ".ai":
            return parent.parent
    return path.parent


def read_state_text(path: Path, *, max_bytes: int = 100_000_000) -> str:
    root = state_root_for_path(path)
    text, _state = read_root_confined_text(
        path,
        root=root,
        max_bytes=max_bytes,
        require_private=False,
    )
    return text


def jsonl_lock_path(path: Path) -> Path:
    path = Path(path)
    return path.with_name(f".{path.name}.lock")


def audit_transaction_lock_path(root: Path) -> Path:
    return Path(root) / ".ai" / "memory" / ".audit-transaction.lock"


def rotation_notice_path(path: Path) -> Path:
    """Return the private sidecar that records lossy JSONL rotation."""
    path = Path(path)
    return path.with_name(f".{path.name}{_ROTATION_NOTICE_SUFFIX}")


def _write_rotation_notice(
    path: Path,
    *,
    root: Path,
    bytes_before: int,
    bytes_after: int,
    lines_after: int,
    dry_run: bool = False,
) -> None:
    if dry_run:
        return
    try:
        source = path.relative_to(root).as_posix()
    except ValueError:
        source = path.as_posix()
    notice = {
        "schema_version": 1,
        "source": source,
        "ts": now_iso(),
        "lossy": True,
        "reason": "bounded_tail_rotation",
        "bytes_before": max(0, int(bytes_before)),
        "bytes_after": max(0, int(bytes_after)),
        "bytes_discarded": max(0, int(bytes_before) - int(bytes_after)),
        "lines_after": max(0, int(lines_after)),
    }
    notice_path = rotation_notice_path(path)
    with private_file_lock(jsonl_lock_path(notice_path), root=root):
        atomic_write_private_text(
            notice_path,
            json.dumps(notice, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
            root=root,
        )


def _trim_jsonl_locked(path: Path, *, root: Path) -> bool:
    try:
        state = validate_root_confined_regular_file(
            path,
            root=root,
            require_owner=True,
            reject_group_other_writable=True,
        )
    except FileNotFoundError:
        return False
    if int(state.st_size) <= _JSONL_AUTO_MAX_BYTES:
        return False
    data, _state, complete = read_root_confined_tail_bytes(
        path,
        root=root,
        max_bytes=_JSONL_AUTO_KEEP_BYTES + _JSONL_LINE_MAX_BYTES + 1,
        require_private=False,
        require_owner=True,
        reject_group_other_writable=True,
    )
    if not complete:
        boundary = data.find(b"\n")
        data = data[boundary + 1:] if boundary >= 0 else b""
    text = data.decode("utf-8")
    lines = text.splitlines()[-_JSONL_AUTO_KEEP_LINES:]
    kept_reversed: list[str] = []
    total = 0
    for candidate in reversed(lines):
        encoded = (candidate + "\n").encode("utf-8")
        if len(encoded) > _JSONL_LINE_MAX_BYTES:
            continue
        if total + len(encoded) > _JSONL_AUTO_KEEP_BYTES:
            break
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        kept_reversed.append(candidate)
        total += len(encoded)
    replacement = "".join(line + "\n" for line in reversed(kept_reversed))
    atomic_write_private_text(path, replacement, root=root)
    _write_rotation_notice(
        path,
        root=root,
        bytes_before=int(state.st_size),
        bytes_after=len(replacement.encode("utf-8")),
        lines_after=len(kept_reversed),
    )
    return True


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path = Path(path)
    root = state_root_for_path(path)
    line = json.dumps(
        record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(line.encode("utf-8")) > _JSONL_LINE_MAX_BYTES:
        raise OSError("JSONL record exceeds line limit")
    with private_file_lock(jsonl_lock_path(path), root=root):
        append_private_text(path, line + "\n", root=root)
        _trim_jsonl_locked(path, root=root)


def decisions_path(root: Path) -> Path:
    return root / ".ai" / "memory" / "decisions.jsonl"


def todos_path(root: Path) -> Path:
    return root / ".ai" / "memory" / "todos.jsonl"


def session_current_path(root: Path) -> Path:
    return root / ".ai" / "memory" / "session-current.md"


def _short_id(prefix: str) -> str:
    import secrets
    return f"{prefix}-{secrets.token_hex(4)}"


FAILURE_STATUSES = ("observed", "confirmed", "stale", "refuted")
_RETIRED_STATUSES = frozenset({"stale", "refuted"})


def _norm_kind(kind: str | None) -> str:
    # unknown coerces to "decision" so a typo can never WIDEN surfacing (fail-safe)
    return "failure" if str(kind or "").strip().lower() == "failure" else "decision"


def _norm_status(status: str | None) -> str:
    s = str(status or "").strip().lower()
    return s if s in FAILURE_STATUSES else "observed"


def _redact_versions(obj: dict[str, str]) -> dict[str, str]:
    """Redact BOTH keys and values (redact_value only recurses values) and clamp."""
    from .redact import redact_value

    out: dict[str, str] = {}
    for k, v in list(obj.items())[:8]:
        ck = str(redact_value(str(k)))[:40].strip()
        cv = str(redact_value(str(v)))[:60]
        if ck:
            out[ck] = cv
    return out


def _decision_id_exists(root: Path, dec_id: str) -> bool:
    for rec in read_jsonl_all(decisions_path(root)):
        if isinstance(rec, dict) and rec.get("id") == dec_id and rec.get("kind") == "failure":
            return True
    return False


def tombstoned_decision_ids(root: Path) -> set[str]:
    """Ids hard-forgotten via a tombstone marker. Fail-soft (empty on read error)."""
    out: set[str] = set()
    try:
        rows = read_jsonl_all(decisions_path(root))
    except Exception:
        return out
    for rec in rows:
        if isinstance(rec, dict) and rec.get("kind") == "tombstone":
            tid = str(rec.get("target_id") or "")
            if tid:
                out.add(tid)
    return out


def _valid_edge_id(value: str | None) -> str | None:
    """Return a decision id only if it looks like one (dec-<hex>); else None (fail-soft).

    Edge ids name other decision records; _short_id mints them as 'dec-' + 8 hex chars.
    Malformed/empty input is ignored silently so a bad edge can never raise or pollute a record.
    """
    s = str(value or "").strip()
    if not s.startswith("dec-"):
        return None
    suffix = s[4:]
    if not suffix or not all(c in "0123456789abcdef" for c in suffix.lower()):
        return None
    return s


_EXPIRES_AT_MAX_CHARS = 32
_ISO_DATE_LEN = 10  # YYYY-MM-DD


def _valid_expires_at(value: str | None) -> str | None:
    """Return a UTC-normalized ISO bound, or None (fail-soft) when the value is malformed.

    _is_expired compares LEXICALLY against now_iso() (a UTC '...Z' string), so an unvalidated
    bound silently misorders: 'expires_at=2026' sorts before every real timestamp, which killed
    the record the instant it was written — no error, no undo. An offset-bearing value is just
    as bad ('2026-07-30T08:00:00-05:00' is an hour in the FUTURE yet lexically precedes
    '2026-07-30T12:00:00Z'), hence the normalization to UTC. A malformed bound is dropped so the
    record simply never expires, exactly how _valid_edge_id drops a malformed edge.

    A date-only bound is accepted and widened to the LAST instant of that day: 'expires_at:
    2026-12-31' reads as "valid through 2026-12-31", not "dead at 2026-12-31T00:00Z" (which
    would make expires_at=<today> expire on arrival — the very bug being fixed). A naive
    (offset-less) datetime is read as UTC, matching now_iso().
    """
    raw = str(redact_value(str(value or ""))).strip()[:_EXPIRES_AT_MAX_CHARS]
    # shape-gate before parsing so acceptance never depends on fromisoformat leniency
    if len(raw) < _ISO_DATE_LEN or raw[4] != "-" or raw[7] != "-":
        return None
    if len(raw) > _ISO_DATE_LEN and raw[_ISO_DATE_LEN] != "T":
        return None
    # OverflowError joins ValueError because the UTC conversion itself can fail at the
    # datetime domain edges ('9999-12-31T23:59:59-14:00' shifts past datetime.max,
    # '0001-01-01T00:00:00+14:00' before datetime.min); both are malformed bounds to drop.
    try:
        dt = datetime.fromisoformat(raw)
        if len(raw) == _ISO_DATE_LEN:
            dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError):
        return None


def _is_expired(rec: dict[str, Any], *, now: str | None = None) -> bool:
    """True when rec carries an expires_at strictly before now (ISO compare). Fail-soft.

    expires_at is an opt-in field; records without it never expire. Comparison is lexical
    on normalized ISO strings (now_iso emits a trailing 'Z'), which orders correctly for
    UTC timestamps; a malformed/empty bound is treated as non-expiring.
    """
    exp = str(rec.get("expires_at") or "").strip()
    if not exp:
        return False
    return exp < (now or now_iso())


def live_decision_records(
    rows: list[Any],
    *,
    now: str | None = None,
    include_retired: bool = False,
    include_expired: bool = False,
) -> list[dict[str, Any]]:
    """Single source of truth for "which decision rows are still live".

    Every path that surfaces decision content to an agent must go through this, otherwise a
    refuted or time-boxed decision leaks back into the injected context through a side door
    (memory_tier's HOT consolidation and recommend's decision-tag mining both did exactly that).
    Rules: failures fold by id — last write wins, so a later 'stale'/'refuted' reappend retires
    the original — retired failures drop unless include_retired, and a past expires_at drops
    unless include_expired. Plain decisions keep file order and folded failures follow them in
    first-seen order, so callers can still partition or re-sort. Fail-soft: non-dict rows and
    id-less failures (unfoldable, so they could duplicate) are skipped.

    Tombstones (hard-forget markers) are handled HERE and only here, deliberately:
      - a tombstone row itself never surfaces AND never consumes a caller's tail
        window (DECISIONS_TAIL is 3 — three forgets rendered as plain rows would
        evict every real decision from the SessionStart block);
      - any row whose id was tombstoned is suppressed in BOTH partitions, with no
        include_* escape — forget is unconditional, unlike retire/expire;
      - suppression is order-independent, so a peer's union merge that re-adds the
        forgotten line (or lands it AFTER the tombstone) changes nothing.
    """
    now_s = now or now_iso()
    tombstoned: set[str] = set()
    for rec in rows:
        if isinstance(rec, dict) and rec.get("kind") == "tombstone":
            tid = str(rec.get("target_id") or "")
            if tid:
                tombstoned.add(tid)
    plain: list[dict[str, Any]] = []
    failures: dict[str, dict[str, Any]] = {}
    for rec in rows:
        if not isinstance(rec, dict):
            continue
        if rec.get("kind") == "tombstone":
            continue
        if tombstoned and str(rec.get("id") or "") in tombstoned:
            continue
        if rec.get("kind") == "failure":
            fid = str(rec.get("id") or "")
            if fid:
                failures[fid] = rec  # fold
        elif include_expired or not _is_expired(rec, now=now_s):
            plain.append(rec)
    out = list(plain)
    for rec in failures.values():
        if not include_retired and str(rec.get("status", "observed")) in _RETIRED_STATUSES:
            continue
        if not include_expired and _is_expired(rec, now=now_s):
            continue
        out.append(rec)
    return out


def append_decision(
    root: Path,
    *,
    text: str,
    tags: list[str] | None = None,
    source: str | None = None,
    kind: str | None = None,
    observed_at: str | None = None,
    observed_versions: dict[str, str] | None = None,
    environment: str | None = None,
    retest_after: str | None = None,
    status: str | None = None,
    supersedes_id: str | None = None,
    contradicts: str | None = None,
    derives_from: str | None = None,
    expires_at: str | None = None,
) -> dict[str, Any]:
    from .redact import redact_value
    text_clean = redact_value(str(text)).strip()
    if not text_clean:
        return {"ok": False, "reason": "empty_text"}
    tag_list = [str(t).strip() for t in (tags or []) if str(t).strip()]
    # legacy plain decisions stay byte-identical: no new keys are written for them.
    record: dict[str, Any] = {
        "id": _short_id("dec"),
        "decided_at": now_iso(),
        "decision": text_clean[:1024],
        "tags": tag_list,
        "source": str(source or "operator")[:64],
    }
    # A hard-forgotten id must never be reborn: ids carry only 32 bits, so a fresh
    # record CAN collide with a tombstoned target — and would then be silently
    # suppressed by live_decision_records on arrival. Regenerate past collisions.
    suppressed = tombstoned_decision_ids(root)
    for _ in range(8):
        if record["id"] not in suppressed:
            break
        record["id"] = _short_id("dec")
    if _norm_kind(kind) == "failure":
        record["kind"] = "failure"
        record["status"] = _norm_status(status)
        if observed_at:
            record["observed_at"] = str(observed_at)[:32]
        if observed_versions and isinstance(observed_versions, dict):
            red = _redact_versions(observed_versions)
            if red:
                record["observed_versions"] = red
        if environment:
            record["environment"] = str(redact_value(str(environment)))[:128]
        if retest_after:
            record["retest_after"] = str(retest_after)[:32]
        # supersession: reuse the target id so the fold-by-id retires the original
        if supersedes_id and _decision_id_exists(root, str(supersedes_id)):
            record["id"] = str(supersedes_id)
    # optional DAG edges (kind-agnostic): stored ONLY when provided so legacy/plain
    # decisions stay byte-identical. Edge ids and the expires_at bound are both validated
    # fail-soft: a malformed value is omitted, never stored and never raised.
    contradicts_id = _valid_edge_id(contradicts)
    if contradicts_id:
        record["contradicts"] = contradicts_id
    derives_id = _valid_edge_id(derives_from)
    if derives_id:
        record["derives_from"] = derives_id
    expires_bound = _valid_expires_at(expires_at)
    if expires_bound:
        record["expires_at"] = expires_bound
    append_jsonl(decisions_path(root), record)
    append_audit(root, action="memory.decision_add", category="memory",
                 payload={"id": record["id"], "kind": record.get("kind", "decision")})
    return {"ok": True, "record": record}


def read_decisions_for_surface(
    root: Path, *, limit: int, include_expired: bool = False
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """One full-file pass → (recent plain decisions, live folded failures newest-first).

    Failures fold by id (last write wins) so a later 'stale'/'refuted' reappend retires the
    original; retired failures are dropped. Plain decisions and failures are partitioned so
    failures never consume the plain tail window and retired rows never leak. Decisions whose
    optional expires_at is in the past are treated as retired and dropped unless include_expired.
    Fail-soft.
    """
    try:
        rows = read_jsonl_all(decisions_path(root))
    except Exception:
        return [], []
    rows_live = live_decision_records(rows, include_expired=include_expired)
    plain = [r for r in rows_live if r.get("kind") != "failure"]
    live = [r for r in rows_live if r.get("kind") == "failure"]
    live.sort(key=lambda r: str(r.get("observed_at") or r.get("decided_at") or ""), reverse=True)
    return plain[-limit:], live


def read_decisions_filtered(
    root: Path,
    *,
    kind: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    source: str | None = None,
    text: str | None = None,
    limit: int = 20,
    include_retired: bool = False,
    include_expired: bool = False,
) -> dict[str, Any]:
    """On-demand filtered read over decisions.jsonl (newest-first).

    Unlike read_decisions_for_surface (which feeds the fixed SessionStart tail), this lets an
    agent query past decisions mid-session. It reuses the same integrity rules so a query can
    never surface a duplicate or retired row: failures fold by id (last write wins) and
    stale/refuted ones drop unless include_retired. Records whose optional expires_at is in the
    past are dropped unless include_expired. Filters are AND-combined — kind/status are exact,
    tag/source/text are case-insensitive substring. Fail-soft → empty on error.
    """
    try:
        rows = read_jsonl_all(decisions_path(root))
    except Exception:
        return {"ok": True, "count": 0, "items": []}

    items = live_decision_records(
        rows, include_retired=include_retired, include_expired=include_expired
    )

    kind_f = (kind or "").strip().lower() or None
    status_f = (status or "").strip().lower() or None
    tag_f = (tag or "").strip().lower() or None
    source_f = (source or "").strip().lower() or None
    text_f = (text or "").strip().lower() or None

    def _match(rec: dict[str, Any]) -> bool:
        rkind = "failure" if rec.get("kind") == "failure" else "decision"
        if kind_f and rkind != kind_f:
            return False
        if status_f and str(rec.get("status", "")).lower() != status_f:
            return False
        if source_f and source_f not in str(rec.get("source", "")).lower():
            return False
        if tag_f and not any(tag_f in str(t).lower() for t in (rec.get("tags") or [])):
            return False
        if text_f and text_f not in str(rec.get("decision", "")).lower():
            return False
        return True

    matched = [r for r in items if _match(r)]
    matched.sort(key=lambda r: str(r.get("observed_at") or r.get("decided_at") or ""), reverse=True)
    n = max(0, int(limit))
    return {"ok": True, "count": len(matched[:n]), "items": matched[:n]}


def append_todo(
    root: Path,
    *,
    title: str,
    owner: str | None = None,
    tags: list[str] | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    from .redact import redact_value
    title_clean = redact_value(str(title)).strip()
    if not title_clean:
        return {"ok": False, "reason": "empty_title"}
    tag_list = [str(t).strip() for t in (tags or []) if str(t).strip()]
    record = {
        "id": _short_id("todo"),
        "title": title_clean[:512],
        "status": "open",
        "owner": str(owner or "")[:64],
        "tags": tag_list,
        "created_at": now_iso(),
        "source": str(source or "operator")[:64],
    }
    append_jsonl(todos_path(root), record)
    append_audit(root, action="memory.todo_add", category="memory", payload={"id": record["id"]})
    return {"ok": True, "record": record}


def close_todo(
    root: Path,
    *,
    match: str,
    status: str = "done",
    reason: str | None = None,
) -> dict[str, Any]:
    """Mark the latest matching open todo as closed. Match is substring on title or exact id.

    Writes a *new* status-update line (append-only); the original open line stays for audit.
    """
    if status not in {"done", "closed", "cancelled", "canceled"}:
        return {"ok": False, "reason": "invalid_status"}
    path = todos_path(root)
    try:
        text = read_state_text(path)
    except (OSError, UnicodeDecodeError):
        return {"ok": False, "reason": "no_todos"}
    candidates: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        eid = str(entry.get("id") or "")
        if not eid:
            title = str(entry.get("title") or entry.get("text") or entry.get("summary") or "").strip()
            if title:
                eid = f"legacy:{title}"
        if not eid:
            continue
        if eid not in candidates:
            order.append(eid)
        candidates[eid] = entry
    target: dict[str, Any] | None = None
    target_key = ""
    needle = match.strip().lower()
    for eid in reversed(order):
        entry = candidates[eid]
        cur_status = str(entry.get("status") or "open").lower()
        if cur_status in {"done", "closed", "completed", "cancelled", "canceled"}:
            continue
        if needle == str(entry.get("id") or "").lower():
            target = entry; target_key = eid; break
        title = str(entry.get("title") or entry.get("text") or "").lower()
        if needle and needle in title:
            target = entry; target_key = eid; break
    if target is None:
        return {"ok": False, "reason": "no_match"}
    # A legacy row can carry no id at all, and it DOES surface as an open todo, so subscripting
    # target["id"] used to raise KeyError on a todo the user could see. Close it under the same
    # synthetic 'legacy:<title>' key the readers derive, so the append actually folds onto the
    # original row and the todo really leaves the open list. Rows that do have an id keep it
    # verbatim (type included), so their update record stays byte-identical.
    target_ref = target.get("id") or target_key
    update = {
        "id": target_ref,
        "title": target.get("title"),
        "status": status,
        "owner": target.get("owner", ""),
        "tags": target.get("tags", []),
        "created_at": target.get("created_at"),
        "closed_at": now_iso(),
        "close_reason": (reason or "")[:240],
        "source": target.get("source", "operator"),
    }
    append_jsonl(path, update)
    append_audit(root, action="memory.todo_close", category="memory", payload={"id": target_ref, "status": status})
    return {"ok": True, "record": update}


_SESSION_NOTE_MAX_BYTES = 102400
_SESSION_NOTE_KEEP_BYTES = 51200


def append_session_note(root: Path, *, text: str) -> dict[str, Any]:
    from .redact import redact_value
    text_clean = redact_value(str(text)).strip()
    if not text_clean:
        return {"ok": False, "reason": "empty_text"}
    root = Path(root)
    path = session_current_path(root)
    line = f"- [{now_iso()}] {text_clean[:1024]}\n"
    line_bytes = line.encode("utf-8")
    max_bytes = max(2048, int(_SESSION_NOTE_MAX_BYTES))
    keep_bytes = max(0, min(int(_SESSION_NOTE_KEEP_BYTES), max_bytes))
    read_cap = max(max_bytes + keep_bytes + 65536, max_bytes * 2)
    header = "# Current Session\n\n"
    lock_path = jsonl_lock_path(path)
    try:
        with private_file_lock(lock_path, root=root):
            recovered = False
            try:
                existing, _state = read_root_confined_text(
                    path,
                    root=root,
                    max_bytes=read_cap,
                    require_private=False,
                    require_owner=True,
                    reject_group_other_writable=True,
                )
            except FileNotFoundError:
                existing = header
            except (OSError, UnicodeDecodeError):
                existing = header
                recovered = True

            raw = existing.encode("utf-8")
            if len(raw) + len(line_bytes) > max_bytes:
                marker = "[recovered]\n" if recovered else "[rotated]\n"
                prefix = (header + marker).encode("utf-8")
                tail_budget = max(0, min(keep_bytes, max_bytes - len(prefix) - len(line_bytes)))
                tail = raw[-tail_budget:] if tail_budget else b""
                newline = tail.find(b"\n")
                if newline >= 0:
                    tail = tail[newline + 1:]
                content = (prefix + tail + line_bytes).decode("utf-8", errors="replace")
            else:
                if recovered and existing == header:
                    existing += "[recovered]\n"
                content = existing + line

            atomic_write_private_text(path, content, root=root)
    except OSError:
        return {"ok": False, "reason": "write_error"}

    try:
        appended_bytes = len(line_bytes)
        relative_path = str(path.relative_to(root))
    except ValueError:
        return {"ok": False, "reason": "write_error"}
    append_audit(root, action="memory.session_append", category="memory", payload={"bytes": len(line)})
    return {"ok": True, "appended_bytes": appended_bytes, "path": relative_path}


def forget_decision(
    root: Path,
    *,
    target_id: str,
    reason: str = "",
    source: str = "operator",
) -> dict[str, Any]:
    """Hard-forget a decision/failure id: tombstone marker + unconditional compaction.

    CLI-only surface — deliberately NOT exposed over MCP: the tool table hardcodes
    destructiveHint for sandbox_execute alone, so a destructive memory tool there
    would ship advertising destructiveHint=false, a false wire contract.

    Mechanics, each load-bearing:
      - the tombstone carries a FRESH tomb-<hex> id and names its victim via
        target_id; reusing the victim's id would collide with the failure fold;
      - suppression happens inside live_decision_records (every reader), with no
        include_* escape — see that docstring for the tail-window rationale;
      - the body is physically removed under the same lock append_jsonl takes, so
        the two readers that legitimately bypass the shared helper (hooks'
        exception fallback, loop_engineering's raw conflict scan) cannot leak it
        and a concurrent append cannot be lost;
      - the audit event carries ids only (decision bodies never enter the hash
        chain), so compaction cannot break chain verification.

    The receipt reports union_merge_restorable=True: .ai/.gitattributes merges
    *.jsonl with merge=union, so a peer clone that still has the old file can
    resurrect the removed LINE. The (also union-merged) tombstone keeps it
    suppressed on every reader, but this is a surfacing guarantee, not
    cryptographic erasure — the bytes may persist in git history and on peers.
    """
    tid = str(target_id or "").strip()
    if not tid:
        return {"ok": False, "reason": "empty_id"}
    root = Path(root)
    path = decisions_path(root)
    tomb: dict[str, Any] = {
        "id": _short_id("tomb"),
        "kind": "tombstone",
        "target_id": tid,
        "decided_at": now_iso(),
        "source": str(redact_value(str(source or "operator")))[:64],
    }
    reason_clean = str(redact_value(str(reason or ""))).strip()
    if reason_clean:
        tomb["reason"] = reason_clean[:200]
    tomb_line = json.dumps(
        tomb, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    removed = 0
    removed_kinds: set[str] = set()
    kept: list[str] = []
    try:
        with private_file_lock(jsonl_lock_path(path), root=root):
            try:
                lines = list(iter_root_confined_text_lines(
                    path,
                    root=root,
                    max_bytes=_JSONL_ALL_MAX_BYTES,
                    max_line_bytes=_JSONL_LINE_MAX_BYTES,
                    require_private=False,
                    require_owner=True,
                    reject_group_other_writable=True,
                ))
            except FileNotFoundError:
                return {"ok": False, "reason": "no_match", "target_id": tid}
            for raw in lines:
                line = raw.rstrip("\n")
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    kept.append(line)  # fail-soft: never destroy what we cannot parse
                    continue
                if (
                    isinstance(rec, dict)
                    and rec.get("kind") != "tombstone"
                    and str(rec.get("id") or "") == tid
                ):
                    removed += 1
                    removed_kinds.add(str(rec.get("kind") or "decision"))
                    continue
                kept.append(line)
            if not removed:
                return {"ok": False, "reason": "no_match", "target_id": tid}
            kept.append(tomb_line)
            atomic_write_private_text(path, "".join(l + "\n" for l in kept), root=root)
    except OSError as exc:
        return {"ok": False, "reason": f"write_error:{exc}"[:200]}
    append_audit(
        root,
        action="memory.decision_forget",
        category="memory",
        payload={"id": tid, "tombstone_id": tomb["id"], "removed_rows": removed},
    )
    return {
        "ok": True,
        "target_id": tid,
        "tombstone_id": tomb["id"],
        "removed_rows": removed,
        "removed_kinds": sorted(removed_kinds),
        "union_merge_restorable": True,
        "note": (
            "peers merging an older decisions.jsonl (merge=union) can restore the removed "
            "line; the tombstone keeps it suppressed on every reader"
        ),
    }


def forget_session_notes(root: Path, *, contains: str) -> dict[str, Any]:
    """Remove session-note lines containing `contains` (-008).

    Session notes are id-less Markdown append logs, so unlike decisions there is
    no id to tombstone — removal is by content match and the receipt (plus a
    counts-only audit event) is the record. resume.json snapshots that embed the
    text are deleted whole: they are regenerable caches, and editing JSON in
    place risks breaking the snapshot schema. The needle must be >=4 non-space
    chars so a slip cannot silently gut the whole log.
    """
    needle = str(contains or "")
    if len(needle.strip()) < 4:
        return {"ok": False, "reason": "needle_too_short_min_4_chars"}
    root = Path(root)
    path = session_current_path(root)
    max_bytes = max(2048, int(_SESSION_NOTE_MAX_BYTES))
    read_cap = max(max_bytes * 2, max_bytes + 65536)
    removed_lines = 0
    try:
        with private_file_lock(jsonl_lock_path(path), root=root):
            try:
                text, _state = read_root_confined_text(
                    path,
                    root=root,
                    max_bytes=read_cap,
                    require_private=False,
                    require_owner=True,
                    reject_group_other_writable=True,
                )
            except FileNotFoundError:
                text = ""
            if text:
                kept_lines = [ln for ln in text.splitlines() if needle not in ln]
                removed_lines = len(text.splitlines()) - len(kept_lines)
                if removed_lines:
                    content = "\n".join(kept_lines)
                    atomic_write_private_text(
                        path, content + "\n" if content else "", root=root
                    )
    except OSError as exc:
        return {"ok": False, "reason": f"write_error:{exc}"[:200]}
    removed_snapshots: list[str] = []
    base = root / ".ai" / "memory" / "sessions"
    if base.exists():
        for snap in sorted(base.glob("*/resume.json")):
            try:
                if needle in snap.read_text(encoding="utf-8", errors="replace"):
                    unlink_root_confined_regular_file(snap, root=root)
                    removed_snapshots.append(str(snap.relative_to(root)))
            except OSError:
                continue
    append_audit(
        root,
        action="memory.session_forget",
        category="memory",
        payload={"removed_lines": removed_lines, "removed_snapshots": len(removed_snapshots)},
    )
    return {
        "ok": True,
        "removed_lines": removed_lines,
        "removed_snapshots": removed_snapshots,
        "git_history_restorable": True,
        "note": (
            "session notes are git-synced; removed lines persist in git history and on "
            "peers until their clones catch up"
        ),
    }


def audit_path(root: Path, *, at: datetime | None = None) -> Path:
    effective = at or datetime.now(timezone.utc)
    return root / ".ai" / "memory" / "audit" / f"{effective.year}.jsonl"


def _audit_file_sort_key(name: str) -> tuple[int, int, int] | None:
    """Return physical audit order for canonical year and segment names."""
    if len(name) == 10 and name[:4].isdigit() and name[4:] == ".jsonl":
        return int(name[:4]), 1, 0  # current file follows immutable segments
    parts = name.split(".")
    if (
        len(parts) == 4
        and len(parts[0]) == 4
        and parts[0].isdigit()
        and len(parts[1]) == 6
        and parts[1].isdigit()
        and len(parts[2]) == 12
        and all(char in "0123456789abcdef" for char in parts[2])
        and parts[3] == "jsonl"
    ):
        return int(parts[0]), 0, int(parts[1])
    return None


def all_audit_files(root: Path) -> list[Path]:
    """Return immutable segments then the current file in physical order.

    Used by lifetime-totals call sites (e.g. surfacing summary, adaptive
    min_signal) that must aggregate across year boundaries. Returns an empty
    list when the audit directory is missing.
    """
    root = Path(root)
    d = root / ".ai" / "memory" / "audit"
    try:
        names = list_root_confined_directory(
            d,
            root=root,
            max_entries=_AUDIT_FILE_MAX_COUNT,
        )
    except (FileNotFoundError, OSError):
        return []
    files: list[tuple[tuple[int, int, int], Path]] = []
    for name in names:
        sort_key = _audit_file_sort_key(name)
        if sort_key is None:
            continue
        path = d / name
        try:
            validate_root_confined_regular_file(
                path,
                root=root,
                require_owner=True,
                reject_group_other_writable=True,
            )
        except (FileNotFoundError, OSError):
            continue
        files.append((sort_key, path))
    # A divergent sync can leave two digest-named segments with the same
    # (year, sequence).  Keep discovery deterministic so every reader sees the
    # same evidence; strict doctor/repair reject the duplicate sequence rather
    # than silently choosing one branch.
    return [path for _key, path in sorted(files, key=lambda item: (item[0], item[1].name))]


def audit_segment_sequence_issues(paths: Iterable[Path]) -> list[dict[str, Any]]:
    """Report duplicate, missing-head, and interior-gap segment sequences.

    Raw segments are never pruned, so every segmented year must start at one
    and remain contiguous. A gap is evidence loss and must never be repaired by
    relinking the remaining physical neighbors.
    """

    by_year: dict[int, dict[int, list[str]]] = {}
    for path in paths:
        key = _audit_file_sort_key(Path(path).name)
        if key is None or key[1] != 0:
            continue
        by_year.setdefault(key[0], {}).setdefault(key[2], []).append(str(path))
    issues: list[dict[str, Any]] = []
    for year, sequence_paths in sorted(by_year.items()):
        for sequence, matches in sorted(sequence_paths.items()):
            if len(matches) > 1:
                issues.append(
                    {
                        "kind": "duplicate",
                        "year": year,
                        "sequence": sequence,
                        "paths": sorted(matches),
                    }
                )
        sequences = sorted(sequence_paths)
        if not sequences:
            continue
        if sequences[0] != 1:
            issues.append(
                {
                    "kind": "start",
                    "year": year,
                    "expected": 1,
                    "actual": sequences[0],
                }
            )
        for previous, current in zip(sequences, sequences[1:]):
            if current != previous + 1:
                issues.append(
                    {
                        "kind": "gap",
                        "year": year,
                        "after": previous,
                        "before": current,
                        "missing_start": previous + 1,
                        "missing_end": current - 1,
                    }
                )
    return issues


def _rotate_audit_chain_locked(root: Path, path: Path, *, incoming_bytes: int = 0) -> bool:
    """Seal the current audit file as an immutable, byte-identical segment.

    Unlike the legacy tail rotation, this operation discards and rewrites no
    raw event. A small marker starts the new current file and cryptographically
    links it to the previous segment. The digest-bearing filename makes a
    crash between segment creation and current-file replacement idempotent.
    """
    try:
        state = validate_root_confined_regular_file(
            path,
            root=root,
            require_owner=True,
            reject_group_other_writable=True,
        )
    except FileNotFoundError:
        return False
    before = int(state.st_size)
    if before <= 0 or before + max(0, int(incoming_bytes)) <= _AUDIT_MAX_BYTES:
        return False
    if before > max(_AUDIT_MAX_BYTES + _AUDIT_LINE_MAX_BYTES, 128 * 1024 * 1024):
        raise OSError("audit current file exceeds safe segmentation bound")
    text, _state = read_root_confined_text(
        path,
        root=root,
        max_bytes=before,
        require_private=False,
        require_owner=True,
        reject_group_other_writable=True,
    )
    encoded = text.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    year = int(path.name[:4])
    existing = all_audit_files(root)
    segments = [
        candidate
        for candidate in existing
        if (key := _audit_file_sort_key(candidate.name)) is not None
        and key[0] == year
        and key[1] == 0
    ]
    segment: Path | None = None
    for candidate in segments:
        if candidate.name.split(".")[2] != digest[:12]:
            continue
        candidate_text, _candidate_state = read_root_confined_text(
            candidate, root=root, max_bytes=before, require_private=False
        )
        if candidate_text == text:
            segment = candidate
            break
    if segment is None:
        segment_keys = [key for candidate in segments if (key := _audit_file_sort_key(candidate.name))]
        next_sequence = max((key[2] for key in segment_keys), default=0) + 1
        if next_sequence > 999_999:
            raise OSError("audit segment sequence exhausted")
        segment = path.with_name(f"{year}.{next_sequence:06d}.{digest[:12]}.jsonl")
        atomic_write_private_text(segment, text, root=root)
        persisted, _persisted_state = read_root_confined_text(
            segment, root=root, max_bytes=before, require_private=False
        )
        if persisted != text:
            raise OSError("audit segment verification failed")

    nonempty_lines = [line for line in text.splitlines() if line.strip()]
    previous_last_sha = line_sha(nonempty_lines[-1]) if nonempty_lines else None
    _marker, marker_line = _bounded_audit_line(
        timestamp=datetime.now(timezone.utc),
        action="audit.segment_started",
        category="storage",
        payload={
            "previous_segment": segment.relative_to(root).as_posix(),
            "previous_file_sha256": digest,
            "previous_last_sha": previous_last_sha,
            "bytes_segmented": before,
            "lossy": False,
        },
        prev_sha=None,
        event_id=_new_audit_event_id(),
        max_bytes=_AUDIT_SEGMENT_MARKER_MAX_BYTES,
    )
    atomic_write_private_text(path, marker_line + "\n", root=root)
    return True


def _prune_old_audit_files_locked(root: Path, *, current_year: int) -> int:
    """Keep raw audit years indefinitely; retention applies only to derived data."""
    # Raw audit is the integrity/provenance source of truth.  Deleting a year
    # here would make a later rollup unverifiable and turn retention into loss.
    del root, current_year
    return 0


def append_audit(root: Path, *, action: str, category: str, payload: dict[str, Any]) -> dict[str, Any]:
    root = Path(root)
    timestamp = datetime.now(timezone.utc)
    path = audit_path(root, at=timestamp)
    with _AUDIT_THREAD_LOCK:
        with private_file_lock(audit_transaction_lock_path(root), root=root):
            with private_file_lock(jsonl_lock_path(path), root=root):
                event_id = _new_audit_event_id()
                prev_sha = _previous_audit_sha(path, root=root)
                record, line = _bounded_audit_line(
                    timestamp=timestamp,
                    action=action,
                    category=category,
                    payload=payload,
                    prev_sha=prev_sha,
                    event_id=event_id,
                )
                rotated = _rotate_audit_chain_locked(
                    root, path, incoming_bytes=len((line + "\n").encode("utf-8"))
                )
                if rotated:
                    prev_sha = _previous_audit_sha(path, root=root)
                    record, line = _bounded_audit_line(
                        timestamp=timestamp,
                        action=action,
                        category=category,
                        payload=payload,
                        prev_sha=prev_sha,
                        event_id=event_id,
                    )
                append_private_text(path, line + "\n", root=root)
            pruned = _prune_old_audit_files_locked(root, current_year=timestamp.year)
            if rotated or pruned:
                _rebuild_audit_index_locked(root)
            else:
                append_jsonl(
                    root / ".ai" / "memory" / "audit-index.jsonl",
                    {
                        "ts": record["ts"],
                        "category": record["category"],
                        "action": record["action"],
                        "path": path.relative_to(root).as_posix(),
                        "event_id": record.get("event_id"),
                    },
                )
    return record


def _rebuild_audit_index_locked(root: Path) -> dict[str, Any]:
    audit_root = root / ".ai" / "memory" / "audit"
    index_path = root / ".ai" / "memory" / "audit-index.jsonl"
    rows: deque[dict[str, Any]] = deque(maxlen=_AUDIT_INDEX_MAX_ROWS)
    skipped = 0
    for path in all_audit_files(root):
        rel = path.relative_to(root).as_posix()
        try:
            with private_file_lock(jsonl_lock_path(path), root=root):
                text, _state = read_root_confined_text(
                    path,
                    root=root,
                    max_bytes=100_000_000,
                    require_private=False,
                )
        except (OSError, UnicodeDecodeError):
            skipped += 1
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not isinstance(record, dict):
                skipped += 1
                continue
            rows.append(
                {
                    "ts": record.get("ts"),
                    "category": record.get("category"),
                    "action": record.get("action"),
                    "path": rel,
                    "event_id": record.get("event_id"),
                }
            )
    text = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in rows)
    with private_file_lock(jsonl_lock_path(index_path), root=root):
        atomic_write_private_text(index_path, text, root=root)
    result: dict[str, Any] = {
        "ok": True,
        "path": index_path.relative_to(root).as_posix(),
        "indexed": len(rows),
    }
    if skipped:
        result["skipped"] = skipped
    return result


def rebuild_audit_index(root: Path) -> dict[str, Any]:
    root = Path(root)
    with _AUDIT_THREAD_LOCK:
        with private_file_lock(audit_transaction_lock_path(root), root=root):
            return _rebuild_audit_index_locked(root)


def read_jsonl_tail(path: Path, limit: int) -> list[dict[str, Any]]:
    try:
        bounded_limit = max(0, min(_JSONL_TAIL_MAX_LIMIT, int(limit)))
    except (TypeError, ValueError, OverflowError):
        bounded_limit = 0
    if bounded_limit <= 0:
        return []
    path = Path(path)
    root = state_root_for_path(path)
    byte_budget = min(
        _JSONL_TAIL_MAX_BYTES,
        max(_JSONL_TAIL_MIN_BYTES, bounded_limit * _JSONL_TAIL_BYTES_PER_ITEM),
    )
    try:
        data, _state, complete = read_root_confined_tail_bytes(
            path,
            root=root,
            max_bytes=byte_budget,
            require_private=False,
            require_owner=True,
            reject_group_other_writable=True,
        )
    except (OSError, UnicodeDecodeError):
        return []
    if not complete:
        boundary = data.find(b"\n")
        if boundary < 0:
            return []
        data = data[boundary + 1:]
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-(bounded_limit * 4):]:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out[-bounded_limit:]


def read_jsonl_open_todos(path: Path, limit: int) -> list[dict[str, Any]]:
    try:
        bounded_limit = max(0, min(_OPEN_TODO_MAX_LIMIT, int(limit)))
    except (TypeError, ValueError, OverflowError):
        bounded_limit = 0
    if bounded_limit <= 0:
        return []
    path = Path(path)
    root = state_root_for_path(path)
    latest: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    seen_records = 0
    try:
        for line in iter_root_confined_text_lines(
            path,
            root=root,
            max_bytes=_JSONL_ALL_MAX_BYTES,
            max_line_bytes=_JSONL_LINE_MAX_BYTES,
            require_private=False,
            require_owner=True,
            reject_group_other_writable=True,
        ):
            line = line.strip()
            if not line:
                continue
            seen_records += 1
            if seen_records > _JSONL_ALL_MAX_RECORDS:
                return []
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            eid = str(entry.get("id") or "")
            if not eid:
                title = str(entry.get("title") or entry.get("text") or entry.get("summary") or "").strip()
                if title:
                    eid = f"legacy:{title}"
            if not eid:
                continue
            if eid not in latest:
                order.append(eid)
            latest[eid] = entry
    except (OSError, UnicodeDecodeError):
        return []
    open_items: list[dict[str, Any]] = []
    for eid in order:
        entry = latest[eid]
        status = str(entry.get("status") or entry.get("state") or "open").lower()
        if status in {"done", "closed", "completed", "cancelled", "canceled"}:
            continue
        open_items.append(entry)
        if len(open_items) >= bounded_limit:
            break
    return open_items


def read_jsonl_all(path: Path) -> list[dict[str, Any]]:
    path = Path(path)
    root = state_root_for_path(path)
    out: list[dict[str, Any]] = []
    seen_records = 0
    try:
        for line in iter_root_confined_text_lines(
            path,
            root=root,
            max_bytes=_JSONL_ALL_MAX_BYTES,
            max_line_bytes=_JSONL_LINE_MAX_BYTES,
            require_private=False,
            require_owner=True,
            reject_group_other_writable=True,
        ):
            line = line.strip()
            if not line:
                continue
            seen_records += 1
            if seen_records > _JSONL_ALL_MAX_RECORDS:
                return []
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                out.append(entry)
    except (OSError, UnicodeDecodeError):
        return []
    return out


def read_text_tail(path: Path, lines: int) -> str:
    try:
        line_cap = max(0, min(_TEXT_TAIL_MAX_LINES, int(lines)))
    except (TypeError, ValueError, OverflowError):
        line_cap = 0
    if line_cap <= 0:
        return ""
    path = Path(path)
    root = state_root_for_path(path)
    byte_budget = min(
        _TEXT_TAIL_MAX_BYTES,
        max(_TEXT_TAIL_MIN_BYTES, line_cap * _TEXT_TAIL_BYTES_PER_LINE),
    )
    try:
        data, _state, complete = read_root_confined_tail_bytes(
            path,
            root=root,
            max_bytes=byte_budget,
            require_private=False,
            require_owner=True,
            reject_group_other_writable=True,
        )
    except OSError:
        return ""
    if not complete:
        boundary = data.find(b"\n")
        if boundary < 0:
            return ""
        data = data[boundary + 1:]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ""
    tail = text.rstrip().splitlines()[-line_cap:]
    return "\n".join(tail)


def rotate_jsonl_tail(
    path: Path,
    *,
    max_bytes: int,
    keep_lines: int,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Keep the newest JSONL tail within both a line and byte budget."""
    path = Path(path)
    root = state_root_for_path(path)
    rel = path.as_posix()
    try:
        byte_cap = max(0, min(_JSONL_ROTATE_MAX_BYTES, int(max_bytes)))
        line_cap = max(0, min(_JSONL_ROTATE_MAX_LINES, int(keep_lines)))
    except (TypeError, ValueError, OverflowError):
        return {"ok": False, "path": rel, "exists": False, "rotated": False, "error": "invalid_bounds"}
    try:
        state = validate_root_confined_regular_file(
            path,
            root=root,
            require_owner=True,
            reject_group_other_writable=True,
        )
        before = int(state.st_size)
    except FileNotFoundError:
        return {"ok": True, "path": rel, "exists": False, "rotated": False, "bytes_before": 0, "bytes_after": 0}
    except OSError as exc:
        return {"ok": False, "path": rel, "exists": True, "rotated": False, "error": type(exc).__name__}
    if before <= byte_cap:
        return {"ok": True, "path": rel, "exists": True, "rotated": False, "bytes_before": before, "bytes_after": before}

    try:
        with private_file_lock(jsonl_lock_path(path), root=root):
            data, state, complete = read_root_confined_tail_bytes(
                path,
                root=root,
                max_bytes=byte_cap + _JSONL_LINE_MAX_BYTES + 1,
                require_private=False,
                require_owner=True,
                reject_group_other_writable=True,
            )
            before = int(state.st_size)
            if before <= byte_cap:
                return {
                    "ok": True,
                    "path": rel,
                    "exists": True,
                    "rotated": False,
                    "bytes_before": before,
                    "bytes_after": before,
                }
            if not complete:
                boundary = data.find(b"\n")
                data = data[boundary + 1:] if boundary >= 0 else b""
            text = data.decode("utf-8")
            lines = text.splitlines()
            tail = lines[-line_cap:] if line_cap else []
            kept_reversed: list[str] = []
            total = 0
            for line in reversed(tail):
                line_bytes = len((line + "\n").encode("utf-8"))
                if line_bytes > byte_cap:
                    continue
                if total + line_bytes > byte_cap:
                    break
                kept_reversed.append(line)
                total += line_bytes
                if total >= byte_cap:
                    break
            kept = list(reversed(kept_reversed))
            replacement = ("\n".join(kept) + "\n") if kept else ""
            after = len(replacement.encode("utf-8"))
            if not dry_run:
                atomic_write_private_text(path, replacement, root=root)
                _write_rotation_notice(
                    path,
                    root=root,
                    bytes_before=before,
                    bytes_after=after,
                    lines_after=len(kept),
                )
            return {
                "ok": True,
                "path": rel,
                "exists": True,
                "rotated": True,
                "dry_run": dry_run,
                "bytes_before": before,
                "bytes_after": after,
                "lines_before": len(lines),
                "lines_after": len(kept),
                "tail_complete": complete,
                "lossy": True,
                "rotation_notice": rotation_notice_path(path).relative_to(root).as_posix(),
            }
    except (OSError, UnicodeDecodeError) as exc:
        return {"ok": False, "path": rel, "exists": True, "rotated": False, "error": type(exc).__name__}


EVENTS_MAX_BYTES = 4_000_000  # events.jsonl is hook telemetry mined only for RECENT command
EVENTS_KEEP = 5000            # patterns — rotate to the most recent N lines, drop the rest.
EVENT_PAYLOAD_MAX_BYTES = 20_000
EVENT_PAYLOAD_PREVIEW_CHARS = 12_000


def events_path(root: Path) -> Path:
    return root / ".ai" / "memory" / "events" / "events.jsonl"


def _bounded_event_payload(event: dict[str, Any]) -> Any:
    payload = redact_value(event)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) <= EVENT_PAYLOAD_MAX_BYTES:
        return payload
    return {
        "truncated": True,
        "original_bytes": len(encoded.encode("utf-8")),
        "preview": encoded[:EVENT_PAYLOAD_PREVIEW_CHARS],
    }


def _maybe_rotate_events(path: Path) -> None:
    """Best-effort: keep events.jsonl bounded to the most recent useful tail.

    events.jsonl is append-only hook telemetry whose only consumer (precall_recommend) mines
    recent command patterns, so unbounded growth is pure waste (it grew to hundreds of MB).
    Rotation fires only above EVENTS_MAX_BYTES, rewrites in place under the same exclusive lock
    appends use, and never raises (telemetry must not break the hook path). Unlike the audit
    log (hash-chained — never truncated), events carry no integrity requirement.
    """
    rotate_jsonl_tail(path, max_bytes=EVENTS_MAX_BYTES, keep_lines=EVENTS_KEEP)


def append_event(root: Path, event: dict[str, Any]) -> dict[str, Any]:
    record = {
        "ts": now_iso(),
        "kind": event.get("hook", event.get("kind", "unknown")),
        "agent": event.get("agent", "unknown"),
        "agent_session_id": event.get("agent_session_id"),
        "payload": _bounded_event_payload(event),
    }
    path = events_path(root)
    append_jsonl(path, record)
    _maybe_rotate_events(path)
    append_audit(root, action="event.append", category="memory", payload={"kind": record["kind"], "agent": record["agent"]})
    return record
