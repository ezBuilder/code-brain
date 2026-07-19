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

import hashlib
import json
import os
import re
import stat as stat_module
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ai_core.context_budget import PROTECTED_SIGNALS, policy as context_budget_policy
from ai_core.private_write import (
    atomic_write_private_text,
    ensure_root_confined_directory,
    private_file_lock,
    read_root_confined_text,
)
from ai_core.redact import redact_value

RESUME_RETENTION_DAYS = 14
try:
    RESUME_MAX_BYTES = max(512, min(8192, int(os.environ.get("AI_RESUME_MAX_BYTES", "4096"))))
except (ValueError, TypeError):
    RESUME_MAX_BYTES = 4096
SCHEMA_VERSION = 1
_SAFE_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")

_DONE_STATUSES = {"done", "closed", "completed", "cancelled", "canceled"}

# Field-drop priority when payload exceeds RESUME_MAX_BYTES.
# Earlier entries are dropped first; later entries are kept longer.
_DROP_ORDER = ("audit_tail_actions", "session_tail", "todos_open")
_PROTECTED_FIELDS = ("handoff", *PROTECTED_SIGNALS)
_SECONDARY_DROP_ORDER = (
    "decisions_tail",
    "forced_reason",
    "resume_hint",
    "context_budget",
    "machine_id",
    "agent",
)
_SESSION_ID_PAYLOAD_MAX_BYTES = 128
_MEMORY_SOURCE_MAX_BYTES = 10_000_000


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_jsonl(path: Path, *, root: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        text, _state = read_root_confined_text(
            path,
            root=root,
            max_bytes=_MEMORY_SOURCE_MAX_BYTES,
            require_private=False,
        )
    except (OSError, UnicodeDecodeError):
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


def _read_text(path: Path, *, root: Path) -> str:
    try:
        text, _state = read_root_confined_text(
            path,
            root=root,
            max_bytes=_MEMORY_SOURCE_MAX_BYTES,
            require_private=False,
        )
        return text
    except (OSError, UnicodeDecodeError):
        return ""


def _decisions_tail(root: Path) -> list[dict[str, Any]]:
    entries = _read_jsonl(root / ".ai" / "memory" / "decisions.jsonl", root=root)
    return entries[-5:]


def _todos_open(root: Path) -> list[dict[str, Any]]:
    entries = _read_jsonl(root / ".ai" / "memory" / "todos.jsonl", root=root)
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
    text = _read_text(root / ".ai" / "memory" / "session-current.md", root=root)
    if not text:
        return ""
    lines = text.splitlines()
    tail = lines[-12:]
    return "\n".join(tail)


def _audit_tail_actions(root: Path) -> list[str]:
    # Real installs store audit under .ai/memory/audit/<year>.jsonl; the flat
    # audit.jsonl never exists there, so the previous read always returned [] and
    # every resume snapshot shipped empty recent-actions. Read the per-year files
    # (newest two) and fall back to the legacy flat file when absent.
    entries: list[dict[str, Any]] = []
    try:
        from .memory import all_audit_files

        files = all_audit_files(root)
    except Exception:
        files = []
    if not files:
        legacy = root / ".ai" / "memory" / "audit.jsonl"
        files = [legacy]
    for path in files[-2:]:
        entries.extend(_read_jsonl(path, root=root))
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


def _snapshot_json(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _payload_size(payload: dict[str, Any]) -> int:
    return len(_snapshot_json(payload).encode("utf-8"))


def _shrink_value_once(value: Any) -> tuple[Any, bool]:
    if isinstance(value, str):
        if not value:
            return value, False
        return value[: max(0, len(value) // 2)], True
    if isinstance(value, list):
        if len(value) > 1:
            return value[: max(1, len(value) // 2)], True
        if len(value) == 1:
            shrunk, changed = _shrink_value_once(value[0])
            if changed:
                return [shrunk], True
            return [], True
        return value, False
    if isinstance(value, dict):
        if not value:
            return value, False
        ordered = sorted(
            value,
            key=lambda key: _payload_size({str(key): value[key]}),
            reverse=True,
        )
        for key in ordered:
            shrunk, changed = _shrink_value_once(value[key])
            if changed:
                updated = dict(value)
                updated[key] = shrunk
                return updated, True
        return {}, True
    return value, False


def _shrink_to_fit(payload: dict[str, Any], cap: int) -> dict[str, Any]:
    cap = int(cap)
    if cap <= 0:
        raise ValueError("snapshot byte cap must be positive")
    result = json.loads(json.dumps(payload, ensure_ascii=False))
    if _payload_size(result) <= cap:
        return result
    for field in (*_DROP_ORDER, *_SECONDARY_DROP_ORDER):
        if field in _PROTECTED_FIELDS:
            continue
        if field in result:
            result.pop(field, None)
            if _payload_size(result) <= cap:
                return result

    # Protected signals remain present, but their values may be compacted when
    # the protected content alone would otherwise violate the hard byte cap.
    for _attempt in range(512):
        if _payload_size(result) <= cap:
            return result
        candidates = [field for field in _PROTECTED_FIELDS if field in result]
        if not candidates:
            break
        field = max(candidates, key=lambda name: _payload_size({name: result[name]}))
        shrunk, changed = _shrink_value_once(result[field])
        if not changed:
            break
        result[field] = shrunk

    # Last-resort compaction of non-protected identity strings. The full
    # session identifier is already represented by session_id_sha256 when it
    # was truncated before composition.
    for field in ("session_id", "written_at", "session_id_sha256"):
        while field in result and _payload_size(result) > cap:
            shrunk, changed = _shrink_value_once(result[field])
            if not changed:
                break
            result[field] = shrunk

    if _payload_size(result) > cap:
        raise ValueError(
            f"resume snapshot cannot fit hard byte cap: {_payload_size(result)} > {cap}"
        )
    return result


def _bounded_utf8_prefix(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _snapshot_session_identity(session_id: str) -> tuple[str, str | None]:
    value = str(session_id or "")
    if len(value.encode("utf-8")) <= _SESSION_ID_PAYLOAD_MAX_BYTES:
        return value, None
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return _bounded_utf8_prefix(value, _SESSION_ID_PAYLOAD_MAX_BYTES), digest


def _session_directory_name(session_id: str) -> str:
    value = str(session_id or "").strip()
    if _SAFE_SESSION_ID.fullmatch(value):
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]
    return f"sid-{digest}"


def _ensure_session_dir(root: Path, session_id: str) -> Path:
    session_dir = root / ".ai" / "memory" / "sessions" / _session_directory_name(session_id)
    return ensure_root_confined_directory(session_dir, root=root, mode=0o700)


# ---------------------------------------------------------------------------
# Machine identity (P2): which machine produced a snapshot (Mac vs VPS, …).
# Stored under the git-IGNORED .ai/cache/ so each machine keeps its own id and it
# never syncs; snapshots then record where they were written for cross-machine
# provenance and "prior thread was on <machine>" handoff hints.
# ---------------------------------------------------------------------------
def _read_machine_id(cache: Path, root: Path) -> str:
    try:
        text, _state = read_root_confined_text(
            cache,
            root=root,
            max_bytes=128,
            require_private=True,
        )
        existing = text.strip()
        if existing:
            return existing[:48]
    except OSError:
        pass
    return ""


def _new_machine_id() -> str:
    # OPAQUE by default — never derive from hostname/username. machine_id is embedded in
    # GIT-TRACKED memory (handoff.json) that travels Mac↔VPS and is publicly distributable,
    # so it must carry NO PII. Users who want a readable label ("mac"/"vps") opt in via
    # AI_MACHINE_LABEL (their explicit choice). The id is persisted in the gitignored
    # .ai/cache/ so it stays stable per machine without ever being committed.
    import os
    import re
    import secrets

    label = os.environ.get("AI_MACHINE_LABEL", "").strip()
    if label:
        mid = re.sub(r"[^A-Za-z0-9_-]+", "-", label).strip("-")[:48] or ("cb-" + secrets.token_hex(4))
    else:
        mid = "cb-" + secrets.token_hex(4)
    return mid


def machine_id(root: Path) -> str:
    root = Path(root)
    cache = root / ".ai" / "cache" / "machine_id"
    existing = _read_machine_id(cache, root)
    if existing:
        return existing
    try:
        with private_file_lock(cache.with_name(".machine_id.lock"), root=root):
            existing = _read_machine_id(cache, root)
            if existing:
                return existing
            mid = _new_machine_id()
            atomic_write_private_text(cache, mid, root=root)
            return mid
    except OSError:
        # Fail-soft when the cache cannot be created; callers still receive an
        # opaque non-PII identifier for the current payload.
        return _new_machine_id()


def _resume_hint(agent: str, session_id: str) -> str:
    """Best-effort command to reopen the agent's native transcript on its origin
    machine. Transcripts never leave their machine (all 3 agents are local-only),
    so this is only a pointer the other machine can act on by hand."""
    a = (agent or "").lower()
    sid = (session_id or "").strip()
    safe_sid = sid if _SAFE_SESSION_ID.fullmatch(sid) else ""
    if a == "claude":
        return f"claude --resume {safe_sid}" if safe_sid else "claude --resume"
    if a == "codex":
        return "codex resume"  # interactive picker; rollout id lives under ~/.codex/sessions
    if a in {"antigravity", "agy"}:
        return f"agy --conversation={safe_sid}" if safe_sid else "agy --continue"
    return ""


# ---------------------------------------------------------------------------
# Handoff (P1): a small, bounded, intent-carrying block (goal / plan / next_step /
# open_questions / blockers). This is the single highest-leverage field for "where
# were we?" on the other machine. Stored git-tracked so it syncs; the latest write
# is the current intent. write_snapshot embeds it (and protects it from the size
# cap) so a resuming session leads with intent, not just recent facts.
# ---------------------------------------------------------------------------
HANDOFF_PATH_PARTS = (".ai", "memory", "handoff.json")
_HANDOFF_GOAL_MAX = 240
_HANDOFF_ITEM_MAX = 200
_HANDOFF_LIST_MAX = 6


def handoff_path(root: Path) -> Path:
    return Path(root).joinpath(*HANDOFF_PATH_PARTS)


def _clean_list(items: Any) -> list[str]:
    out: list[str] = []
    if isinstance(items, (list, tuple)):
        for it in items:
            s = str(it).strip()[:_HANDOFF_ITEM_MAX]
            if s:
                out.append(s)
            if len(out) >= _HANDOFF_LIST_MAX:
                break
    return out


def read_handoff(root: Path) -> dict[str, Any]:
    path = handoff_path(root)
    try:
        text, _state = read_root_confined_text(
            path,
            root=root,
            max_bytes=65536,
            require_private=False,
        )
        obj = json.loads(text)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _write_handoff_unlocked(
    root: Path,
    *,
    goal: str | None = None,
    plan: list[str] | None = None,
    next_step: str | None = None,
    open_questions: list[str] | None = None,
    blockers: list[str] | None = None,
    agent: str = "operator",
    clear: bool = False,
) -> dict[str, Any]:
    """Partial-update the current handoff (only provided fields change). ``clear``
    wipes it. Atomic, redacted, git-tracked so it travels Mac↔VPS."""
    root = Path(root)
    path = handoff_path(root)
    current: dict[str, Any] = {} if clear else read_handoff(root)
    if goal is not None:
        current["goal"] = str(goal).strip()[:_HANDOFF_GOAL_MAX]
    if next_step is not None:
        current["next_step"] = str(next_step).strip()[:_HANDOFF_GOAL_MAX]
    if plan is not None:
        current["plan"] = _clean_list(plan)
    if open_questions is not None:
        current["open_questions"] = _clean_list(open_questions)
    if blockers is not None:
        current["blockers"] = _clean_list(blockers)
    current["updated_at"] = _utc_now_iso()
    current["agent"] = (agent or "operator")[:32]
    current["machine_id"] = machine_id(root)
    current = redact_value(current)
    atomic_write_private_text(
        path,
        json.dumps(current, ensure_ascii=False, indent=2),
        root=root,
    )
    return {"ok": True, "path": str(path), "handoff": current}


def write_handoff(
    root: Path,
    *,
    goal: str | None = None,
    plan: list[str] | None = None,
    next_step: str | None = None,
    open_questions: list[str] | None = None,
    blockers: list[str] | None = None,
    agent: str = "operator",
    clear: bool = False,
) -> dict[str, Any]:
    root = Path(root)
    lock_path = handoff_path(root).with_name(".handoff.lock")
    with private_file_lock(lock_path, root=root):
        return _write_handoff_unlocked(
            root,
            goal=goal,
            plan=plan,
            next_step=next_step,
            open_questions=open_questions,
            blockers=blockers,
            agent=agent,
            clear=clear,
        )


def _handoff_for_snapshot(root: Path) -> dict[str, Any]:
    """The handoff to embed in a snapshot: the recorded one, or a minimal fallback
    derived from the newest open todo so a resume still leads with intent."""
    h = read_handoff(root)
    has_intent = bool(h.get("goal") or h.get("next_step") or h.get("plan"))
    if has_intent:
        return {k: h[k] for k in ("goal", "plan", "next_step", "open_questions", "blockers", "updated_at") if h.get(k)}
    todos = _todos_open(root)
    if todos:
        newest = todos[0]
        title = str(newest.get("title") or newest.get("text") or newest.get("summary") or "").strip()[:_HANDOFF_GOAL_MAX]
        if title:
            return {"next_step": title, "derived_from": "open_todo"}
    return {}


def write_snapshot(
    root: Path,
    *,
    session_id: str,
    agent: str,
    force: bool = False,
    reason: str | None = None,
    context_budget_mode: str = "balanced",
) -> dict[str, Any]:
    """Compose, redact, size-cap, and atomically write a resume snapshot.

    `force=True` is currently a marker (every write fully recomposes anyway), but
    it is recorded in the snapshot's `forced_reason` so PreCompact / SessionEnd
    audits stay distinguishable from regular session boundaries.
    """

    root = Path(root)
    session_dir = _ensure_session_dir(root, session_id)
    target = session_dir / "resume.json"
    budget = context_budget_policy(context_budget_mode, base_max_bytes=RESUME_MAX_BYTES)
    payload_session_id, session_id_sha256 = _snapshot_session_identity(session_id)

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "session_id": payload_session_id,
        "agent": str(agent or "operator")[:64],
        "context_budget": budget,
        # P2: provenance — which machine + how to reopen the native transcript there.
        "machine_id": machine_id(root),
        "resume_hint": _resume_hint(agent, session_id),
        "written_at": _utc_now_iso(),
        # P1: intent-carrying handoff, placed first and protected from the size cap.
        "handoff": _handoff_for_snapshot(root),
        "decisions_tail": _decisions_tail(root),
        "todos_open": _todos_open(root),
        "session_tail": _session_tail(root),
        "audit_tail_actions": _audit_tail_actions(root),
    }
    if session_id_sha256 is not None:
        payload["session_id_sha256"] = session_id_sha256
        payload["session_id_truncated"] = True
    if not payload["handoff"]:
        payload.pop("handoff")
    if force:
        payload["forced_reason"] = (reason or "force")[:64]

    # Redact every string value before persisting.
    payload = redact_value(payload)

    # Enforce hard size cap by dropping fields in priority order.
    payload = _shrink_to_fit(payload, int(budget["max_bytes"]))

    data = _snapshot_json(payload)
    encoded = data.encode("utf-8")
    if len(encoded) > int(budget["max_bytes"]):
        raise ValueError(
            f"resume snapshot exceeded hard byte cap after compaction: "
            f"{len(encoded)} > {budget['max_bytes']}"
        )

    atomic_write_private_text(target, data, root=root)

    return {"ok": True, "path": str(target), "bytes_written": len(encoded)}


def read_latest_snapshot(
    root: Path,
    *,
    exclude_session_id: str | None = None,
) -> dict[str, Any] | None:
    """Return the newest resume snapshot, optionally skipping a session id."""

    root = Path(root)
    base = root / ".ai" / "memory" / "sessions"
    try:
        base_state = base.lstat()
    except OSError:
        return None
    if not stat_module.S_ISDIR(base_state.st_mode) or stat_module.S_ISLNK(base_state.st_mode):
        return None

    excluded_dir_name = (
        _session_directory_name(exclude_session_id)
        if exclude_session_id is not None
        else None
    )
    candidates: list[tuple[float, Path]] = []
    for session_dir in base.iterdir():
        try:
            session_state = session_dir.lstat()
        except OSError:
            continue
        if not stat_module.S_ISDIR(session_state.st_mode) or stat_module.S_ISLNK(session_state.st_mode):
            continue
        if excluded_dir_name is not None and session_dir.name == excluded_dir_name:
            continue
        snap = session_dir / "resume.json"
        try:
            snap_state = snap.lstat()
        except OSError:
            continue
        if not stat_module.S_ISREG(snap_state.st_mode) or stat_module.S_ISLNK(snap_state.st_mode):
            continue
        candidates.append((snap_state.st_mtime, snap))

    candidates.sort(key=lambda t: t[0], reverse=True)
    for _, snap in candidates:
        try:
            text, _state = read_root_confined_text(
                snap,
                root=root,
                max_bytes=max(RESUME_MAX_BYTES, 8192),
                require_private=False,
            )
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
    try:
        base_state = base.lstat()
    except OSError:
        return {"ok": True, "removed": 0, "kept": 0}
    if not stat_module.S_ISDIR(base_state.st_mode) or stat_module.S_ISLNK(base_state.st_mode):
        return {"ok": True, "removed": 0, "kept": 0}

    cutoff = time.time() - max(0, int(older_than_days)) * 86400
    removed = 0
    kept = 0
    for session_dir in base.iterdir():
        try:
            session_state = session_dir.lstat()
        except OSError:
            continue
        if not stat_module.S_ISDIR(session_state.st_mode) or stat_module.S_ISLNK(session_state.st_mode):
            continue
        snap = session_dir / "resume.json"
        try:
            snap_state = snap.lstat()
        except OSError:
            continue
        if not stat_module.S_ISREG(snap_state.st_mode) or stat_module.S_ISLNK(snap_state.st_mode):
            continue
        if snap_state.st_mtime < cutoff:
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
