from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .portable import lock_exclusive_blocking, unlock
from .redact import redact_value

_AUDIT_THREAD_LOCK = threading.RLock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def line_sha(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def _lock_exclusive(handle: Any) -> None:
    lock_exclusive_blocking(handle)


def _unlock(handle: Any) -> None:
    unlock(handle)


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with path.open("a", encoding="utf-8") as handle:
        _lock_exclusive(handle)
        try:
            handle.write(line + "\n")
        finally:
            _unlock(handle)


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
    append_jsonl(decisions_path(root), record)
    append_audit(root, action="memory.decision_add", category="memory",
                 payload={"id": record["id"], "kind": record.get("kind", "decision")})
    return {"ok": True, "record": record}


def read_decisions_for_surface(root: Path, *, limit: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """One full-file pass → (recent plain decisions, live folded failures newest-first).

    Failures fold by id (last write wins) so a later 'stale'/'refuted' reappend retires the
    original; retired failures are dropped. Plain decisions and failures are partitioned so
    failures never consume the plain tail window and retired rows never leak. Fail-soft.
    """
    plain: list[dict[str, Any]] = []
    failures: dict[str, dict[str, Any]] = {}
    try:
        rows = read_jsonl_all(decisions_path(root))
    except Exception:
        return [], []
    for rec in rows:
        if not isinstance(rec, dict):
            continue
        if rec.get("kind") == "failure":
            fid = str(rec.get("id") or "")
            if fid:
                failures[fid] = rec  # fold
        else:
            plain.append(rec)
    live = [r for r in failures.values() if str(r.get("status", "observed")) not in _RETIRED_STATUSES]
    live.sort(key=lambda r: str(r.get("observed_at") or r.get("decided_at") or ""), reverse=True)
    return plain[-limit:], live


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
    if not path.exists():
        return {"ok": False, "reason": "no_todos"}
    candidates: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
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
    path = session_current_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = f"- [{now_iso()}] {text_clean[:1024]}\n"
    if not path.exists():
        path.write_text("# Current Session\n\n", encoding="utf-8")
    else:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size > _SESSION_NOTE_MAX_BYTES:
            try:
                raw = path.read_bytes()
                tail = raw[-_SESSION_NOTE_KEEP_BYTES:]
                nl = tail.find(b"\n")
                if nl >= 0:
                    tail = tail[nl + 1:]
                path.write_bytes(b"# Current Session\n\n[rotated]\n" + tail)
            except OSError:
                pass
    with path.open("a", encoding="utf-8") as handle:
        _lock_exclusive(handle)
        try:
            handle.write(line)
        finally:
            _unlock(handle)
    append_audit(root, action="memory.session_append", category="memory", payload={"bytes": len(line)})
    return {"ok": True, "appended_bytes": len(line.encode("utf-8")), "path": str(path.relative_to(root))}


def audit_path(root: Path, *, at: datetime | None = None) -> Path:
    effective = at or datetime.now(timezone.utc)
    return root / ".ai" / "memory" / "audit" / f"{effective.year}.jsonl"


def all_audit_files(root: Path) -> list[Path]:
    """Return all per-year audit jsonl files sorted ascending.

    Used by lifetime-totals call sites (e.g. surfacing summary, adaptive
    min_signal) that must aggregate across year boundaries. Returns an empty
    list when the audit directory is missing.
    """
    d = root / ".ai" / "memory" / "audit"
    if not d.is_dir():
        return []
    return sorted(d.glob("*.jsonl"))


def append_audit(root: Path, *, action: str, category: str, payload: dict[str, Any]) -> dict[str, Any]:
    timestamp = datetime.now(timezone.utc)
    path = audit_path(root, at=timestamp)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _AUDIT_THREAD_LOCK:
        with path.open("a+", encoding="utf-8") as handle:
            _lock_exclusive(handle)
            try:
                handle.seek(0)
                previous_lines = [line for line in handle.read().splitlines() if line.strip()]
                prev_sha = line_sha(previous_lines[-1]) if previous_lines else None
                record = {
                    "ts": timestamp.isoformat().replace("+00:00", "Z"),
                    "monotonic_ns": time.monotonic_ns(),
                    "action": action,
                    "category": category,
                    "payload": redact_value(payload),
                    "prev_sha": prev_sha,
                }
                line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.seek(0, 2)
                handle.write(line + "\n")
            finally:
                _unlock(handle)
    append_jsonl(
        root / ".ai" / "memory" / "audit-index.jsonl",
        {"ts": record["ts"], "category": category, "action": action, "path": path.relative_to(root).as_posix()},
    )
    return record


def rebuild_audit_index(root: Path) -> dict[str, Any]:
    audit_root = root / ".ai" / "memory" / "audit"
    index_path = root / ".ai" / "memory" / "audit-index.jsonl"
    rows: list[dict[str, Any]] = []
    for path in sorted(audit_root.glob("*.jsonl")):
        rel = path.relative_to(root).as_posix()
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                continue
            rows.append(
                {
                    "ts": record.get("ts"),
                    "category": record.get("category"),
                    "action": record.get("action"),
                    "path": rel,
                }
            )
    index_path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in rows)
    index_path.write_text(text, encoding="utf-8")
    return {"ok": True, "path": index_path.relative_to(root).as_posix(), "indexed": len(rows)}


def read_jsonl_tail(path: Path, limit: int) -> list[dict[str, Any]]:
    if not path.exists() or limit <= 0:
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-(limit * 4):]:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out[-limit:]


def read_jsonl_open_todos(path: Path, limit: int) -> list[dict[str, Any]]:
    if not path.exists() or limit <= 0:
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    latest: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for line in lines:
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
        if eid not in latest:
            order.append(eid)
        latest[eid] = entry
    open_items: list[dict[str, Any]] = []
    for eid in order:
        entry = latest[eid]
        status = str(entry.get("status") or entry.get("state") or "open").lower()
        if status in {"done", "closed", "completed", "cancelled", "canceled"}:
            continue
        open_items.append(entry)
        if len(open_items) >= limit:
            break
    return open_items


def read_jsonl_all(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    out: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def read_text_tail(path: Path, lines: int) -> str:
    if not path.exists() or lines <= 0:
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    tail = text.rstrip().splitlines()[-lines:]
    return "\n".join(tail)


def rotate_jsonl_tail(
    path: Path,
    *,
    max_bytes: int,
    keep_lines: int,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Keep the newest JSONL tail within both a line and byte budget."""
    rel = path.as_posix()
    try:
        before = path.stat().st_size
    except OSError:
        return {"ok": True, "path": rel, "exists": False, "rotated": False, "bytes_before": 0, "bytes_after": 0}
    if before <= max_bytes:
        return {"ok": True, "path": rel, "exists": True, "rotated": False, "bytes_before": before, "bytes_after": before}

    try:
        with path.open("r+", encoding="utf-8") as handle:
            _lock_exclusive(handle)
            try:
                lines = handle.read().splitlines()
                tail = lines[-max(1, int(keep_lines)):]
                kept_reversed: list[str] = []
                total = 0
                for line in reversed(tail):
                    line_bytes = len((line + "\n").encode("utf-8"))
                    if kept_reversed and total + line_bytes > max_bytes:
                        break
                    kept_reversed.append(line)
                    total += line_bytes
                    if total >= max_bytes:
                        break
                if not kept_reversed and tail:
                    kept_reversed.append(tail[-1])
                    total = len((tail[-1] + "\n").encode("utf-8"))
                kept = list(reversed(kept_reversed))
                after = total if kept else 0
                if not dry_run:
                    handle.seek(0)
                    handle.write(("\n".join(kept) + "\n") if kept else "")
                    handle.truncate()
                    after = path.stat().st_size
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
                }
            finally:
                _unlock(handle)
    except OSError as exc:
        return {"ok": False, "path": rel, "exists": True, "rotated": False, "error": str(exc)}


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
