"""Worker registry — the single truth surface for loopd's warm worker inventory (PRD §6.1).

Append-only JSONL folded by worker_id (last write wins), mirroring the decisions/todos
pattern. Stores identity, tmux mapping, declared model/profile, usage hints and lifecycle
state — never secret/auth values (paths and status only). Heartbeats are separate small
files updated by the wrapper/process check, never by an LLM. stdlib only, fail-soft.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .memory import append_audit, append_jsonl, now_iso, read_jsonl_all

WORKERS_PARTS = (".ai", "runtime", "state", "workers.jsonl")
HEARTBEAT_PARTS = (".ai", "runtime", "state", "heartbeats")

STATES = (
    "booting", "idle", "assigned", "working", "reviewing",
    "blocked", "quota_exhausted", "auth_required", "stale", "lost", "stopped",
)
# states that can receive new work
ASSIGNABLE = frozenset({"idle"})
# states that are alive but unavailable (do not re-dispatch, do not treat as lost)
PARKED = frozenset({"blocked", "quota_exhausted", "auth_required"})


def workers_path(root: Path) -> Path:
    return root.joinpath(*WORKERS_PARTS)


def heartbeat_path(root: Path, worker_id: str) -> Path:
    safe = "".join(c for c in str(worker_id) if c.isalnum() or c in "-_")[:64] or "unknown"
    return root.joinpath(*HEARTBEAT_PARTS, f"{safe}.json")


def _bounded(value: Any, limit: int) -> str:
    return str(value if value is not None else "")[:limit]


def register_worker(
    root: Path,
    *,
    worker_id: str,
    agent: str,
    profile: str = "",
    project_root: str = "",
    cwd: str = "",
    pane_id: str = "",
    session: str = "",
    window: str = "",
    pid: int | None = None,
    model: dict[str, Any] | None = None,
    capabilities: list[str] | None = None,
    risk_tier_allowed: list[str] | None = None,
    state: str = "booting",
) -> dict[str, Any]:
    wid = _bounded(worker_id, 64)
    if not wid:
        raise ValueError("worker_id is required")
    if state not in STATES:
        raise ValueError(f"invalid state: {state}")
    record = {
        "schema_version": 1,
        "worker_id": wid,
        "agent": _bounded(agent, 32),
        "profile": _bounded(profile, 64),
        "isolation_id": _bounded(profile, 64),
        "project_root": _bounded(project_root, 512),
        "cwd": _bounded(cwd, 512),
        "tmux": {"session": _bounded(session, 64), "window": _bounded(window, 64),
                 "pane_id": _bounded(pane_id, 32), "pid": int(pid) if isinstance(pid, int) else None},
        "model": model if isinstance(model, dict) else {"source": "wrapper-config"},
        "usage": {"requests_today": 0, "quota_state": "unknown", "last_usage_source": "none"},
        "state": state,
        "capabilities": [_bounded(c, 32) for c in (capabilities or ["code_edit", "review", "research"])][:16],
        "risk_tier_allowed": [r for r in (risk_tier_allowed or ["low", "medium"]) if r in ("low", "medium", "high")],
        "current_request_id": None,
        "heartbeat_at": now_iso(),
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    append_jsonl(workers_path(root), record)
    append_audit(root, action="worker.register", category="loopd",
                 payload={"worker_id": wid, "agent": record["agent"], "profile": record["profile"]})
    return {"ok": True, "worker": record}


def _folded(root: Path) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for rec in read_jsonl_all(workers_path(root)):
        if isinstance(rec, dict) and rec.get("worker_id"):
            latest[str(rec["worker_id"])] = rec
    return latest


def get_worker(root: Path, worker_id: str) -> dict[str, Any] | None:
    return _folded(root).get(str(worker_id))


def list_workers(root: Path, *, state: str | None = None) -> list[dict[str, Any]]:
    items = [r for r in _folded(root).values() if r.get("state") != "stopped"]
    if state is not None:
        items = [r for r in items if r.get("state") == state]
    return sorted(items, key=lambda r: str(r.get("worker_id", "")))


# fields a caller may mutate after registration; everything else is set only at register_worker.
_UPDATABLE = frozenset({
    "state", "current_request_id", "usage", "model", "heartbeat_at",
    "tmux", "cwd", "pid",
})


def update_worker(root: Path, *, worker_id: str, **fields: Any) -> dict[str, Any]:
    current = _folded(root).get(str(worker_id))
    if current is None:
        return {"ok": False, "reason": "not_found", "worker_id": worker_id}
    if "state" in fields and fields["state"] not in STATES:
        return {"ok": False, "reason": "invalid_state", "state": fields["state"]}
    rejected = [k for k in fields if k not in _UPDATABLE]
    if rejected:
        return {"ok": False, "reason": "field_not_updatable", "fields": rejected}
    updated = dict(current)
    updated.update({k: v for k, v in fields.items()})
    updated["updated_at"] = now_iso()
    append_jsonl(workers_path(root), updated)
    return {"ok": True, "worker": updated}


def set_state(root: Path, *, worker_id: str, state: str, request_id: str | None = None) -> dict[str, Any]:
    if state not in STATES:
        return {"ok": False, "reason": "invalid_state", "state": state}
    fields: dict[str, Any] = {"state": state}
    if request_id is not None or state in ("assigned", "working", "reviewing"):
        fields["current_request_id"] = request_id
    res = update_worker(root, worker_id=worker_id, **fields)
    if res.get("ok"):
        append_audit(root, action="worker.state", category="loopd",
                     payload={"worker_id": worker_id, "state": state})
    return res


def write_heartbeat(root: Path, *, worker_id: str, state: str, request_id: str | None = None,
                    pane_id: str = "", last_output_hash: str = "", last_error: str | None = None) -> dict[str, Any]:
    """Wrapper/process-driven heartbeat (no LLM). Updates both the beat file and folded state."""
    beat = {
        "worker_id": _bounded(worker_id, 64),
        "state": state if state in STATES else "idle",
        "request_id": _bounded(request_id, 64) if request_id else None,
        "tmux_pane_id": _bounded(pane_id, 32),
        "last_seen_at": now_iso(),
        "last_output_hash": _bounded(last_output_hash, 80),
        "last_error": _bounded(last_error, 240) if last_error else None,
    }
    path = heartbeat_path(root, worker_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(beat, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return {"ok": True, "heartbeat": beat}


def read_heartbeat(root: Path, worker_id: str) -> dict[str, Any] | None:
    try:
        return json.loads(heartbeat_path(root, worker_id).read_text(encoding="utf-8"))
    except Exception:
        return None
