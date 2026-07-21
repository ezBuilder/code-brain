from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .loss_accounting import finalize_event, loss_event
from .portable import lock_exclusive_blocking, unlock
from .private_write import (
    append_private_text,
    atomic_write_private_text,
    private_file_lock,
    read_root_confined_text,
)
from .redact import redact_value

_AUDIT_THREAD_LOCK = threading.RLock()

# Audit logs are operational state, not an unlimited archive. Keep each
# yearly file bounded and retain only a small rolling year window.
AUDIT_MAX_BYTES = 16_000_000
AUDIT_KEEP_BYTES = 12_000_000
AUDIT_RETENTION_YEARS = 3
AUDIT_PAYLOAD_MAX_BYTES = 64_000
AUDIT_PAYLOAD_PREVIEW_BYTES = 16_000
AUDIT_RETENTION_SCAN_MAX_CANDIDATES = 1024
AUDIT_RETENTION_SCAN_MAX_SECONDS = 1.0


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def line_sha(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def _lock_exclusive(handle: Any) -> None:
    lock_exclusive_blocking(handle)


def _unlock(handle: Any) -> None:
    unlock(handle)


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


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path = Path(path)
    root = state_root_for_path(path)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
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
_SESSION_NOTE_READ_MAX_BYTES = 16_000_000


def append_session_note(root: Path, *, text: str) -> dict[str, Any]:
    from .redact import redact_value
    text_clean = redact_value(str(text)).strip()
    if not text_clean:
        return {"ok": False, "reason": "empty_text"}
    path = session_current_path(root)
    line = f"- [{now_iso()}] {text_clean[:1024]}\n"
    rotation_loss: dict[str, Any] | None = None
    lock_path = path.with_name(".session-current.lock")
    try:
        with private_file_lock(lock_path, root=root):
            before_bytes = 0
            try:
                existing, _state = read_root_confined_text(
                    path,
                    root=root,
                    max_bytes=_SESSION_NOTE_READ_MAX_BYTES,
                    require_private=True,
                )
                before_bytes = len(existing.encode("utf-8"))
            except FileNotFoundError:
                existing = "# Current Session\n\n"
                atomic_write_private_text(path, existing, root=root)
                before_bytes = len(existing.encode("utf-8"))
            if before_bytes > _SESSION_NOTE_MAX_BYTES:
                raw = existing.encode("utf-8")
                tail = raw[-_SESSION_NOTE_KEEP_BYTES:]
                nl = tail.find(b"\n")
                if nl >= 0:
                    tail = tail[nl + 1:]
                replacement = b"# Current Session\n\n[rotated]\n" + tail
                replacement_text = replacement.decode("utf-8", errors="ignore")
                atomic_write_private_text(path, replacement_text, root=root)
                after_rotation_bytes = len(replacement_text.encode("utf-8"))
                rotation_loss = finalize_event(
                    root,
                    loss_event(
                        domain="session_rotation",
                        operation=".ai/memory/session-current.md",
                        applied=True,
                        files_before=1,
                        files_after=1,
                        bytes_before=before_bytes,
                        bytes_after=after_rotation_bytes,
                        reasons={"size_limit": 1},
                        examples=("session-current.md",),
                    ),
                )
            append_private_text(path, line, root=root)
    except (OSError, PermissionError) as exc:
        return {
            "ok": False,
            "reason": "session_note_write_failed",
            "error": str(exc)[:200],
            "path": str(path.relative_to(root)),
        }
    append_audit(root, action="memory.session_append", category="memory", payload={"bytes": len(line)})
    accounting_ok = (
        rotation_loss is None
        or rotation_loss.get("accounting", {}).get("ok") is True
    )
    return {
        "ok": accounting_ok,
        "appended_bytes": len(line.encode("utf-8")),
        "path": str(path.relative_to(root)),
        "rotation_loss": rotation_loss,
        "reason": None if accounting_ok else "loss_accounting_failed",
    }


def audit_path(root: Path, *, at: datetime | None = None) -> Path:
    effective = at or datetime.now(timezone.utc)
    return root / ".ai" / "memory" / "audit" / f"{effective.year}.jsonl"


def audit_maintenance_lock_path(root: Path) -> Path:
    # Keep the lock outside audit/ so rebuild can safely ignore a malicious or
    # broken audit-directory symlink and still repair the standalone index.
    return root / ".ai" / "memory" / ".audit-maintenance.lock"


def all_audit_files(root: Path) -> list[Path]:
    """Return all per-year audit jsonl files sorted ascending.

    Used by lifetime-totals call sites (e.g. surfacing summary, adaptive
    min_signal) that must aggregate across year boundaries. Returns an empty
    list when the audit directory is missing.
    """
    d = root / ".ai" / "memory" / "audit"
    try:
        state = d.lstat()
    except OSError:
        return []
    import stat as stat_module

    if not stat_module.S_ISDIR(state.st_mode) or stat_module.S_ISLNK(state.st_mode):
        return []
    files: list[Path] = []
    for path in sorted(d.glob("*.jsonl")):
        try:
            item_state = path.lstat()
        except OSError:
            continue
        if stat_module.S_ISREG(item_state.st_mode) and not stat_module.S_ISLNK(item_state.st_mode):
            files.append(path)
    return files


def _json_line(record: dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _bounded_audit_payload(payload: dict[str, Any]) -> Any:
    redacted = redact_value(payload)
    encoded = _json_line({"payload": redacted}).encode("utf-8")
    if len(encoded) <= AUDIT_PAYLOAD_MAX_BYTES:
        return redacted
    preview = encoded[: max(0, int(AUDIT_PAYLOAD_PREVIEW_BYTES))].decode("utf-8", errors="ignore")
    return {
        "truncated": True,
        "original_bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "preview": preview,
    }


def _chained_line(record: dict[str, Any], previous_line: str | None) -> str:
    chained = dict(record)
    chained["prev_sha"] = line_sha(previous_line) if previous_line is not None else None
    return _json_line(chained)


def _record_ts(raw_line: str) -> str | None:
    try:
        loaded = json.loads(raw_line)
    except json.JSONDecodeError:
        return None
    if not isinstance(loaded, dict):
        return None
    value = loaded.get("ts")
    return value if isinstance(value, str) else None


def _render_rechained(lines: list[str], checkpoint: dict[str, Any]) -> str:
    rendered: list[str] = []
    previous: str | None = None
    for item in [checkpoint, *lines]:
        if isinstance(item, str):
            try:
                loaded = json.loads(item)
            except json.JSONDecodeError:
                rendered.append(item)
                previous = item
                continue
            if not isinstance(loaded, dict):
                rendered.append(item)
                previous = item
                continue
            record = loaded
        else:
            record = item
        line = _chained_line(record, previous)
        rendered.append(line)
        previous = line
    return "\n".join(rendered) + "\n"


def _compact_audit_text(
    text: str,
    *,
    path: Path,
    timestamp: datetime,
    reserve_bytes: int,
) -> tuple[str, dict[str, Any] | None]:
    before = len(text.encode("utf-8"))
    cap = max(1024, int(AUDIT_MAX_BYTES))
    if before + max(0, int(reserve_bytes)) <= cap:
        return text, None

    lines = [line for line in text.splitlines() if line.strip()]
    source_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    tail_budget = max(
        0,
        min(
            max(0, int(AUDIT_KEEP_BYTES)),
            cap - max(0, int(reserve_bytes)) - 1024,
        ),
    )
    selected_reversed: list[str] = []
    selected_bytes = 0
    for line in reversed(lines):
        line_bytes = len((line + "\n").encode("utf-8"))
        if selected_reversed and selected_bytes + line_bytes > tail_budget:
            break
        if line_bytes > tail_budget and not selected_reversed:
            break
        selected_reversed.append(line)
        selected_bytes += line_bytes
    selected = list(reversed(selected_reversed))

    def build_checkpoint(retained: list[str]) -> dict[str, Any]:
        dropped_count = len(lines) - len(retained)
        dropped = lines[:dropped_count]
        return {
            "ts": timestamp.isoformat().replace("+00:00", "Z"),
            "monotonic_ns": time.monotonic_ns(),
            "action": "audit.retention_compact",
            "category": "audit",
            "payload": {
                "path": path.name,
                "bytes_before": before,
                "source_sha256": source_sha,
                "dropped_records": dropped_count,
                "retained_records": len(retained),
                "first_dropped_ts": _record_ts(dropped[0]) if dropped else None,
                "last_dropped_ts": _record_ts(dropped[-1]) if dropped else None,
            },
        }

    checkpoint = build_checkpoint(selected)
    replacement = _render_rechained(selected, checkpoint)
    while selected and len(replacement.encode("utf-8")) + reserve_bytes > cap:
        selected.pop(0)
        checkpoint = build_checkpoint(selected)
        replacement = _render_rechained(selected, checkpoint)

    return replacement, {
        "bytes_before": before,
        "bytes_after": len(replacement.encode("utf-8")),
        "dropped_records": len(lines) - len(selected),
        "retained_records": len(selected),
    }


def _prune_expired_audit_files(
    root: Path,
    *,
    current_year: int,
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    keep_years = max(1, int(AUDIT_RETENTION_YEARS))
    oldest_year = current_year - keep_years + 1
    removed: list[dict[str, Any]] = []
    errors: list[str] = []
    directory = audit_path(root).parent
    policy = {
        "max_candidates": int(AUDIT_RETENTION_SCAN_MAX_CANDIDATES),
        "max_seconds": float(AUDIT_RETENTION_SCAN_MAX_SECONDS),
    }
    if not directory.exists():
        return [], [], {
            "bounded": True,
            "complete": True,
            "candidates_scanned": 0,
            "files_before": 0,
            "files_after": 0,
            "bytes_before": 0,
            "bytes_after": 0,
            "policy": policy,
        }

    started = time.monotonic()
    deadline = started + max(0.05, float(AUDIT_RETENTION_SCAN_MAX_SECONDS))
    candidates_scanned = 0
    files: list[tuple[Path, os.stat_result]] = []
    complete = True
    try:
        entries = os.scandir(directory)
    except OSError as exc:
        return [], [f"list:{exc}"], {
            "bounded": True,
            "complete": False,
            "candidates_scanned": 0,
            "files_before": 0,
            "files_after": 0,
            "bytes_before": 0,
            "bytes_after": 0,
            "policy": policy,
        }
    try:
        with entries:
            for entry in entries:
                if candidates_scanned >= max(1, int(AUDIT_RETENTION_SCAN_MAX_CANDIDATES)):
                    errors.append("scan:candidate_limit")
                    complete = False
                    break
                if time.monotonic() >= deadline:
                    errors.append("scan:time_limit")
                    complete = False
                    break
                candidates_scanned += 1
                if not entry.name.endswith(".jsonl"):
                    continue
                path = Path(entry.path)
                try:
                    state = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    errors.append(f"{entry.name}:stat:{exc}")
                    complete = False
                    continue
                if stat.S_ISLNK(state.st_mode):
                    errors.append(f"{entry.name}:unsafe-symlink")
                    complete = False
                    continue
                if not stat.S_ISREG(state.st_mode):
                    errors.append(f"{entry.name}:not-regular")
                    complete = False
                    continue
                if int(getattr(state, "st_nlink", 1)) != 1:
                    errors.append(f"{entry.name}:unsafe-hardlink")
                    complete = False
                    continue
                files.append((path, state))
    except OSError as exc:
        errors.append(f"scan:{exc}")
        complete = False

    before_files = len(files)
    before_bytes = sum(int(state.st_size) for _path, state in files)
    elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
    if not complete or errors:
        return [], errors or ["scan_incomplete"], {
            "bounded": True,
            "complete": False,
            "candidates_scanned": candidates_scanned,
            "files_before": before_files,
            "files_after": before_files,
            "bytes_before": before_bytes,
            "bytes_after": before_bytes,
            "elapsed_ms": elapsed_ms,
            "policy": policy,
        }

    for path, expected in sorted(files, key=lambda item: item[0].name):
        stem = path.stem
        if len(stem) < 4 or not stem[:4].isdigit() or int(stem[:4]) >= oldest_year:
            continue
        try:
            current = path.lstat()
            if (
                stat.S_ISLNK(current.st_mode)
                or not stat.S_ISREG(current.st_mode)
                or int(getattr(current, "st_nlink", 1)) != 1
                or int(current.st_dev) != int(expected.st_dev)
                or int(current.st_ino) != int(expected.st_ino)
            ):
                errors.append(f"{path.name}:changed-before-delete")
                continue
            path.unlink()
            removed.append({"path": path.name, "bytes": int(expected.st_size)})
        except OSError as exc:
            errors.append(f"{path.name}:{exc}")
    removed_bytes = sum(int(item["bytes"]) for item in removed)
    return removed, errors, {
        "bounded": True,
        "complete": True,
        "candidates_scanned": candidates_scanned,
        "files_before": before_files,
        "files_after": max(0, before_files - len(removed)),
        "bytes_before": before_bytes,
        "bytes_after": max(0, before_bytes - removed_bytes),
        "elapsed_ms": max(0, int((time.monotonic() - started) * 1000)),
        "policy": policy,
    }


def append_audit(root: Path, *, action: str, category: str, payload: dict[str, Any]) -> dict[str, Any]:
    timestamp = datetime.now(timezone.utc)
    path = audit_path(root, at=timestamp)
    with _AUDIT_THREAD_LOCK:
        with private_file_lock(audit_maintenance_lock_path(root), root=root):
            removed_files, retention_errors, retention_scan = _prune_expired_audit_files(
                root,
                current_year=timestamp.year,
            )
            maintenance_losses: list[dict[str, Any]] = []
            if removed_files or retention_errors:
                maintenance_losses.append(
                    finalize_event(
                        root,
                        loss_event(
                            domain="audit_retention",
                            operation=".ai/memory/audit",
                            applied=bool(removed_files) and not retention_errors,
                            files_before=int(retention_scan.get("files_before") or 0),
                            files_after=int(retention_scan.get("files_after") or 0),
                            bytes_before=int(retention_scan.get("bytes_before") or 0),
                            bytes_after=int(retention_scan.get("bytes_after") or 0),
                            reasons={"age_limit": len(removed_files)},
                            errors=retention_errors,
                            examples=(item.get("path") for item in removed_files),
                        ),
                    )
                )
            bounded_payload = _bounded_audit_payload(payload)
            if isinstance(bounded_payload, dict) and bounded_payload.get("truncated") is True:
                original_bytes = int(bounded_payload.get("original_bytes") or 0)
                retained_bytes = len(_json_line({"payload": bounded_payload}).encode("utf-8"))
                maintenance_losses.append(
                    finalize_event(
                        root,
                        loss_event(
                            domain="payload_truncation",
                            operation=f"audit:{action}"[:240],
                            applied=True,
                            bytes_before=original_bytes,
                            bytes_after=retained_bytes,
                            records_before=1,
                            records_after=1,
                            reasons={"payload_limit": 1},
                            examples=(action,),
                        ),
                    )
                )
            base_record = {
                "ts": timestamp.isoformat().replace("+00:00", "Z"),
                "monotonic_ns": time.monotonic_ns(),
                "action": action,
                "category": category,
                "payload": bounded_payload,
            }
            maintenance_record: dict[str, Any] | None = None
            if removed_files or retention_errors:
                maintenance_record = {
                    "ts": base_record["ts"],
                    "monotonic_ns": time.monotonic_ns(),
                    "action": "audit.retention_prune",
                    "category": "audit",
                    "payload": _bounded_audit_payload(
                        {
                            "retention_years": max(1, int(AUDIT_RETENTION_YEARS)),
                            "removed_files": removed_files,
                            "errors": retention_errors,
                            "scan": retention_scan,
                        }
                    ),
                }
            reserve_records = [record for record in (maintenance_record, base_record) if record is not None]
            reserve_bytes = sum(len(_chained_line(record, "0" * 64).encode("utf-8")) + 1 for record in reserve_records)
            compacted = False
            with private_file_lock(jsonl_lock_path(path), root=root):
                try:
                    text, _state = read_root_confined_text(
                        path,
                        root=root,
                        max_bytes=max(100_000_000, int(AUDIT_MAX_BYTES) * 8),
                        require_private=False,
                    )
                except FileNotFoundError:
                    text = ""
                replacement, compact_result = _compact_audit_text(
                    text,
                    path=path,
                    timestamp=timestamp,
                    reserve_bytes=reserve_bytes,
                )
                if compact_result is not None:
                    atomic_write_private_text(path, replacement, root=root)
                    text = replacement
                    compacted = True
                    dropped_records = int(compact_result.get("dropped_records") or 0)
                    retained_records = int(compact_result.get("retained_records") or 0)
                    maintenance_losses.append(
                        finalize_event(
                            root,
                            loss_event(
                                domain="audit_compaction",
                                operation=path.relative_to(root).as_posix(),
                                applied=True,
                                files_before=1,
                                files_after=1,
                                bytes_before=int(compact_result.get("bytes_before") or 0),
                                bytes_after=int(compact_result.get("bytes_after") or 0),
                                records_before=dropped_records + retained_records,
                                records_after=retained_records,
                                reasons={"size_limit": dropped_records},
                                examples=(path.name,),
                            ),
                        )
                    )
                previous_lines = [line for line in text.splitlines() if line.strip()]
                previous_line = previous_lines[-1] if previous_lines else None
                appended_lines: list[str] = []
                for pending in reserve_records:
                    line = _chained_line(pending, previous_line)
                    appended_lines.append(line)
                    previous_line = line
                append_private_text(path, "\n".join(appended_lines) + "\n", root=root)
                record = json.loads(appended_lines[-1])
                if maintenance_losses:
                    record["_maintenance"] = {"losses": maintenance_losses}

            if compacted or removed_files or retention_errors:
                _rebuild_audit_index_locked(root)
            else:
                append_jsonl(
                    root / ".ai" / "memory" / "audit-index.jsonl",
                    {"ts": record["ts"], "category": category, "action": action, "path": path.relative_to(root).as_posix()},
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
    with _AUDIT_THREAD_LOCK:
        with private_file_lock(audit_maintenance_lock_path(root), root=root):
            return _rebuild_audit_index_locked(root)


def read_jsonl_tail(path: Path, limit: int) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    try:
        lines = read_state_text(path).splitlines()
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
    if limit <= 0:
        return []
    try:
        lines = read_state_text(path).splitlines()
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
    try:
        lines = read_state_text(path).splitlines()
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
    if lines <= 0:
        return ""
    try:
        text = read_state_text(path)
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
    path = Path(path)
    root = state_root_for_path(path)
    rel = path.as_posix()
    try:
        operation = path.relative_to(root).as_posix()
    except ValueError:
        operation = path.name

    def with_loss(
        payload: dict[str, Any],
        *,
        before_bytes: int = 0,
        after_bytes: int = 0,
        before_records: int = 0,
        after_records: int = 0,
        errors: list[str] | tuple[str, ...] = (),
    ) -> dict[str, Any]:
        event = finalize_event(
            root,
            loss_event(
                domain="jsonl_rotation",
                operation=operation,
                applied=bool(payload.get("ok")) and not dry_run,
                dry_run=dry_run,
                files_before=1 if payload.get("exists") else 0,
                files_after=1 if payload.get("exists") else 0,
                bytes_before=before_bytes,
                bytes_after=after_bytes,
                records_before=before_records,
                records_after=after_records,
                reasons={"size_limit": max(0, before_records - after_records)},
                errors=errors,
                examples=(operation,),
            ),
        )
        return {**payload, "loss": event}
    try:
        text, state = read_root_confined_text(
            path,
            root=root,
            max_bytes=max(100_000_000, int(max_bytes) * 8),
            require_private=False,
        )
        before = int(state.st_size)
    except FileNotFoundError:
        return with_loss({"ok": True, "path": rel, "exists": False, "rotated": False, "bytes_before": 0, "bytes_after": 0})
    except (OSError, UnicodeDecodeError) as exc:
        return with_loss(
            {"ok": False, "path": rel, "exists": True, "rotated": False, "error": str(exc)},
            errors=(str(exc),),
        )
    if before <= max_bytes:
        line_count = len(text.splitlines())
        return with_loss(
            {"ok": True, "path": rel, "exists": True, "rotated": False, "bytes_before": before, "bytes_after": before},
            before_bytes=before,
            after_bytes=before,
            before_records=line_count,
            after_records=line_count,
        )

    try:
        with private_file_lock(jsonl_lock_path(path), root=root):
            # Re-read under the same lock used by append_jsonl so concurrent
            # appends cannot be lost between the size check and replacement.
            text, state = read_root_confined_text(
                path,
                root=root,
                max_bytes=max(100_000_000, int(max_bytes) * 8),
                require_private=False,
            )
            before = int(state.st_size)
            if before <= max_bytes:
                line_count = len(text.splitlines())
                return with_loss({
                    "ok": True,
                    "path": rel,
                    "exists": True,
                    "rotated": False,
                    "bytes_before": before,
                    "bytes_after": before,
                }, before_bytes=before, after_bytes=before, before_records=line_count, after_records=line_count)
            lines = text.splitlines()
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
            replacement = ("\n".join(kept) + "\n") if kept else ""
            after = len(replacement.encode("utf-8"))
            if not dry_run:
                atomic_write_private_text(path, replacement, root=root)
            return with_loss({
                "ok": True,
                "path": rel,
                "exists": True,
                "rotated": True,
                "dry_run": dry_run,
                "bytes_before": before,
                "bytes_after": after,
                "lines_before": len(lines),
                "lines_after": len(kept),
            }, before_bytes=before, after_bytes=after, before_records=len(lines), after_records=len(kept))
    except OSError as exc:
        return with_loss(
            {"ok": False, "path": rel, "exists": True, "rotated": False, "error": str(exc)},
            before_bytes=before if "before" in locals() else 0,
            after_bytes=before if "before" in locals() else 0,
            errors=(str(exc),),
        )


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
    bounded_payload = _bounded_event_payload(event)
    record = {
        "ts": now_iso(),
        "kind": event.get("hook", event.get("kind", "unknown")),
        "agent": event.get("agent", "unknown"),
        "agent_session_id": event.get("agent_session_id"),
        "payload": bounded_payload,
    }
    path = events_path(root)
    append_jsonl(path, record)
    rotation = rotate_jsonl_tail(path, max_bytes=EVENTS_MAX_BYTES, keep_lines=EVENTS_KEEP)
    losses: list[dict[str, Any]] = []
    if isinstance(bounded_payload, dict) and bounded_payload.get("truncated") is True:
        original_bytes = int(bounded_payload.get("original_bytes") or 0)
        retained_bytes = len(
            json.dumps(bounded_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        losses.append(
            finalize_event(
                root,
                loss_event(
                    domain="payload_truncation",
                    operation=f"event:{record['kind']}"[:240],
                    applied=True,
                    bytes_before=original_bytes,
                    bytes_after=retained_bytes,
                    records_before=1,
                    records_after=1,
                    reasons={"payload_limit": 1},
                    examples=(record["kind"],),
                ),
            )
        )
    rotation_loss = rotation.get("loss") if isinstance(rotation, dict) else None
    if isinstance(rotation_loss, dict):
        losses.append(rotation_loss)
    if losses:
        record["_maintenance"] = {"losses": losses}
    append_audit(root, action="event.append", category="memory", payload={"kind": record["kind"], "agent": record["agent"]})
    return record
