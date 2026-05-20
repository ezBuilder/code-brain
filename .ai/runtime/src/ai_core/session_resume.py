"""Per-session resume snapshots.

Persist a small, redacted JSON snapshot of a session's tail state under
``<root>/.ai/memory/sessions/<session_id>/resume.json`` so that a fresh
Claude/Codex session (e.g. after compaction) can recover the prior session's
context.

Public API:
    write_snapshot(root, *, session_id, agent) -> dict
    read_latest_snapshot(root, *, exclude_session_id=None) -> dict | None
    prune_snapshots(root, *, older_than_days=RESUME_RETENTION_DAYS) -> dict
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ai_core.redact import redact_value

RESUME_RETENTION_DAYS = 14
try:
    RESUME_MAX_BYTES = max(512, min(8192, int(os.environ.get("AI_RESUME_MAX_BYTES", "4096"))))
except (ValueError, TypeError):
    RESUME_MAX_BYTES = 4096
SCHEMA_VERSION = 1

_DONE_STATUSES = {"done", "closed", "completed", "cancelled", "canceled"}

# Field-drop priority when payload exceeds RESUME_MAX_BYTES.
# Earlier entries are dropped first; later entries are kept longer.
_DROP_ORDER = ("audit_tail_actions", "session_tail", "todos_open")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _decisions_tail(root: Path) -> list[dict[str, Any]]:
    entries = _read_jsonl(root / ".ai" / "memory" / "decisions.jsonl")
    return entries[-5:]


def _todos_open(root: Path) -> list[dict[str, Any]]:
    entries = _read_jsonl(root / ".ai" / "memory" / "todos.jsonl")
    latest: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in entries:
        eid = str(item.get("id") or "")
        if not eid:
            title = str(item.get("title") or item.get("text") or item.get("summary") or "").strip()
            if title:
                eid = f"legacy:{title}"
        if not eid:
            continue
        if eid not in latest:
            order.append(eid)
        latest[eid] = item
    open_items: list[dict[str, Any]] = []
    for eid in order:
        item = latest[eid]
        status = str(item.get("status", "")).strip().lower()
        if status in _DONE_STATUSES:
            continue
        open_items.append(item)
    return open_items[-5:]


def _session_tail(root: Path) -> str:
    text = _read_text(root / ".ai" / "memory" / "session-current.md")
    if not text:
        return ""
    lines = text.splitlines()
    tail = lines[-12:]
    return "\n".join(tail)


def _audit_tail_actions(root: Path) -> list[str]:
    entries = _read_jsonl(root / ".ai" / "memory" / "audit.jsonl")
    seen: set[str] = set()
    actions: list[str] = []
    # Walk from newest to oldest so most recent unique actions are preferred.
    for item in reversed(entries):
        action = item.get("action")
        if not isinstance(action, str) or not action:
            continue
        if action in seen:
            continue
        seen.add(action)
        actions.append(action)
        if len(actions) >= 10:
            break
    # Reverse so emission order matches chronological (oldest unique first).
    return list(reversed(actions))


def _payload_size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def _shrink_to_fit(payload: dict[str, Any], cap: int) -> dict[str, Any]:
    if _payload_size(payload) <= cap:
        return payload
    for field in _DROP_ORDER:
        if field in payload:
            # Replace with empty container while still indicating drop.
            payload.pop(field, None)
            if _payload_size(payload) <= cap:
                return payload
    return payload


def _ensure_session_dir(root: Path, session_id: str) -> Path:
    session_dir = root / ".ai" / "memory" / "sessions" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(session_dir, 0o700)
    except OSError:
        pass
    return session_dir


def write_snapshot(
    root: Path,
    *,
    session_id: str,
    agent: str,
    force: bool = False,
    reason: str | None = None,
) -> dict[str, Any]:
    """Compose, redact, size-cap, and atomically write a resume snapshot.

    `force=True` is currently a marker (every write fully recomposes anyway), but
    it is recorded in the snapshot's `forced_reason` so PreCompact / SessionEnd
    audits stay distinguishable from regular session boundaries.
    """

    root = Path(root)
    session_dir = _ensure_session_dir(root, session_id)
    target = session_dir / "resume.json"
    tmp = session_dir / "resume.json.tmp"

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "agent": agent,
        "written_at": _utc_now_iso(),
        "decisions_tail": _decisions_tail(root),
        "todos_open": _todos_open(root),
        "session_tail": _session_tail(root),
        "audit_tail_actions": _audit_tail_actions(root),
    }
    if force:
        payload["forced_reason"] = (reason or "force")[:64]

    # Redact every string value before persisting.
    payload = redact_value(payload)

    # Enforce hard size cap by dropping fields in priority order.
    payload = _shrink_to_fit(payload, RESUME_MAX_BYTES)

    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    encoded = data.encode("utf-8")

    # Atomic write: tmp -> rename. Cleanup tmp on any failure.
    try:
        with open(tmp, "wb") as fh:
            fh.write(encoded)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, target)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise

    try:
        os.chmod(target, 0o600)
    except OSError:
        pass

    return {"ok": True, "path": str(target), "bytes_written": len(encoded)}


def read_latest_snapshot(
    root: Path,
    *,
    exclude_session_id: str | None = None,
) -> dict[str, Any] | None:
    """Return the newest resume snapshot, optionally skipping a session id."""

    root = Path(root)
    base = root / ".ai" / "memory" / "sessions"
    if not base.is_dir():
        return None

    candidates: list[tuple[float, Path]] = []
    for session_dir in base.iterdir():
        if not session_dir.is_dir():
            continue
        if exclude_session_id is not None and session_dir.name == exclude_session_id:
            continue
        snap = session_dir / "resume.json"
        if not snap.is_file():
            continue
        try:
            mtime = snap.stat().st_mtime
        except OSError:
            continue
        candidates.append((mtime, snap))

    candidates.sort(key=lambda t: t[0], reverse=True)
    for _, snap in candidates:
        try:
            text = snap.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            obj = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


def prune_snapshots(
    root: Path,
    *,
    older_than_days: int = RESUME_RETENTION_DAYS,
) -> dict[str, Any]:
    """Delete resume.json files older than ``older_than_days`` (by mtime)."""

    root = Path(root)
    base = root / ".ai" / "memory" / "sessions"
    if not base.is_dir():
        return {"ok": True, "removed": 0, "kept": 0}

    cutoff = time.time() - max(0, int(older_than_days)) * 86400
    removed = 0
    kept = 0
    for session_dir in base.iterdir():
        if not session_dir.is_dir():
            continue
        snap = session_dir / "resume.json"
        if not snap.is_file():
            continue
        try:
            mtime = snap.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            try:
                snap.unlink()
                removed += 1
            except OSError:
                kept += 1
        else:
            kept += 1
    return {"ok": True, "removed": removed, "kept": kept}


__all__ = [
    "RESUME_RETENTION_DAYS",
    "RESUME_MAX_BYTES",
    "write_snapshot",
    "read_latest_snapshot",
    "prune_snapshots",
]
