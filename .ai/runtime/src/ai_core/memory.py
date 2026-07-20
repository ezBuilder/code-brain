from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .private_write import (
    append_private_text,
    atomic_write_private_text,
    iter_root_confined_text_lines,
    list_root_confined_directory,
    private_file_lock,
    read_root_confined_tail_bytes,
    read_root_confined_text,
    validate_root_confined_regular_file,
)
from .redact import redact_value

_AUDIT_THREAD_LOCK = threading.RLock()
_AUDIT_FILE_MAX_COUNT = 256
_AUDIT_LINE_MAX_BYTES = 1_000_000
_AUDIT_ACTION_MAX_CHARS = 256
_AUDIT_CATEGORY_MAX_CHARS = 128
_JSONL_TAIL_MAX_LIMIT = 1_000
_JSONL_TAIL_MIN_BYTES = 256 * 1024
_JSONL_TAIL_MAX_BYTES = 8 * 1024 * 1024
_JSONL_TAIL_BYTES_PER_ITEM = 64 * 1024
_JSONL_LINE_MAX_BYTES = 1_000_000
_JSONL_ROTATE_MAX_BYTES = 100_000_000
_JSONL_ROTATE_MAX_LINES = 100_000
_TEXT_TAIL_MAX_LINES = 1_000
_TEXT_TAIL_MIN_BYTES = 64 * 1024
_TEXT_TAIL_MAX_BYTES = 8 * 1024 * 1024
_TEXT_TAIL_BYTES_PER_LINE = 64 * 1024
_JSONL_ALL_MAX_BYTES = 100_000_000
_JSONL_ALL_MAX_RECORDS = 100_000
_OPEN_TODO_MAX_LIMIT = 1_000


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def line_sha(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def _previous_audit_sha(path: Path, *, root: Path) -> str | None:
    try:
        data, _state, complete = read_root_confined_tail_bytes(
            path,
            root=root,
            max_bytes=_AUDIT_LINE_MAX_BYTES + 1,
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
    if len(last.encode("utf-8")) > _AUDIT_LINE_MAX_BYTES:
        raise OSError("previous audit record exceeds line limit")
    return line_sha(last)


def _bounded_audit_line(
    *,
    timestamp: datetime,
    action: object,
    category: object,
    payload: object,
    prev_sha: str | None,
) -> tuple[dict[str, Any], str]:
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
    line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    encoded = line.encode("utf-8")
    if len(encoded) <= _AUDIT_LINE_MAX_BYTES:
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
    if len(line.encode("utf-8")) > _AUDIT_LINE_MAX_BYTES:
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
    # decisions stay byte-identical. Edge ids are validated (fail-soft); expires_at is a date.
    contradicts_id = _valid_edge_id(contradicts)
    if contradicts_id:
        record["contradicts"] = contradicts_id
    derives_id = _valid_edge_id(derives_from)
    if derives_id:
        record["derives_from"] = derives_id
    if expires_at:
        exp_clean = str(redact_value(str(expires_at))).strip()[:32]
        if exp_clean:
            record["expires_at"] = exp_clean
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
    plain: list[dict[str, Any]] = []
    failures: dict[str, dict[str, Any]] = {}
    try:
        rows = read_jsonl_all(decisions_path(root))
    except Exception:
        return [], []
    now = now_iso()
    for rec in rows:
        if not isinstance(rec, dict):
            continue
        if rec.get("kind") == "failure":
            fid = str(rec.get("id") or "")
            if fid:
                failures[fid] = rec  # fold
        else:
            plain.append(rec)
    if not include_expired:
        plain = [r for r in plain if not _is_expired(r, now=now)]
    live = [
        r for r in failures.values()
        if str(r.get("status", "observed")) not in _RETIRED_STATUSES
        and (include_expired or not _is_expired(r, now=now))
    ]
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

    now = now_iso()
    plain: list[dict[str, Any]] = []
    failures: dict[str, dict[str, Any]] = {}
    for rec in rows:
        if not isinstance(rec, dict):
            continue
        if rec.get("kind") == "failure":
            fid = str(rec.get("id") or "")
            if fid:
                failures[fid] = rec  # fold by id
        else:
            plain.append(rec)

    items: list[dict[str, Any]] = [
        r for r in plain if include_expired or not _is_expired(r, now=now)
    ]
    for rec in failures.values():
        if not include_retired and str(rec.get("status", "observed")) in _RETIRED_STATUSES:
            continue
        if not include_expired and _is_expired(rec, now=now):
            continue
        items.append(rec)

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
    needle = match.strip().lower()
    for eid in reversed(order):
        entry = candidates[eid]
        cur_status = str(entry.get("status") or "open").lower()
        if cur_status in {"done", "closed", "completed", "cancelled", "canceled"}:
            continue
        if needle == str(entry.get("id") or "").lower():
            target = entry; break
        title = str(entry.get("title") or entry.get("text") or "").lower()
        if needle and needle in title:
            target = entry; break
    if target is None:
        return {"ok": False, "reason": "no_match"}
    update = {
        "id": target["id"],
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
    append_audit(root, action="memory.todo_close", category="memory", payload={"id": target["id"], "status": status})
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


def audit_path(root: Path, *, at: datetime | None = None) -> Path:
    effective = at or datetime.now(timezone.utc)
    return root / ".ai" / "memory" / "audit" / f"{effective.year}.jsonl"


def all_audit_files(root: Path) -> list[Path]:
    """Return all per-year audit jsonl files sorted ascending.

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
    files: list[Path] = []
    for name in names:
        if len(name) != 10 or not name[:4].isdigit() or name[4:] != ".jsonl":
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
        files.append(path)
    return files


def append_audit(root: Path, *, action: str, category: str, payload: dict[str, Any]) -> dict[str, Any]:
    root = Path(root)
    timestamp = datetime.now(timezone.utc)
    path = audit_path(root, at=timestamp)
    with _AUDIT_THREAD_LOCK:
        with private_file_lock(audit_transaction_lock_path(root), root=root):
            with private_file_lock(jsonl_lock_path(path), root=root):
                prev_sha = _previous_audit_sha(path, root=root)
                record, line = _bounded_audit_line(
                    timestamp=timestamp,
                    action=action,
                    category=category,
                    payload=payload,
                    prev_sha=prev_sha,
                )
                append_private_text(path, line + "\n", root=root)
            append_jsonl(
                root / ".ai" / "memory" / "audit-index.jsonl",
                {
                    "ts": record["ts"],
                    "category": record["category"],
                    "action": record["action"],
                    "path": path.relative_to(root).as_posix(),
                },
            )
    return record


def _rebuild_audit_index_locked(root: Path) -> dict[str, Any]:
    audit_root = root / ".ai" / "memory" / "audit"
    index_path = root / ".ai" / "memory" / "audit-index.jsonl"
    rows: list[dict[str, Any]] = []
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
