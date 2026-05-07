from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .redact import redact_value

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]

_AUDIT_THREAD_LOCK = threading.RLock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def line_sha(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def _lock_exclusive(handle: Any) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock(handle: Any) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with path.open("a", encoding="utf-8") as handle:
        _lock_exclusive(handle)
        try:
            handle.write(line + "\n")
        finally:
            _unlock(handle)


def audit_path(root: Path, *, at: datetime | None = None) -> Path:
    effective = at or datetime.now(timezone.utc)
    return root / ".ai" / "memory" / "audit" / f"{effective.year}.jsonl"


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


def append_event(root: Path, event: dict[str, Any]) -> dict[str, Any]:
    record = {
        "ts": now_iso(),
        "kind": event.get("hook", event.get("kind", "unknown")),
        "agent": event.get("agent", "unknown"),
        "agent_session_id": event.get("agent_session_id"),
        "payload": redact_value(event),
    }
    append_jsonl(root / ".ai" / "memory" / "events" / "events.jsonl", record)
    append_audit(root, action="event.append", category="memory", payload={"kind": record["kind"], "agent": record["agent"]})
    return record
