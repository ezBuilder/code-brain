from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .doctor import as_payload, check_index_freshness, run_checks
from .hooks import handle_hook
from .context_budget import policy as context_budget_policy
from .search import context_pack, db_path, iter_text_files, rebuild


def index_status(root: Path) -> dict[str, Any]:
    path = db_path(root)
    source_mtime = newest_source_mtime(root)
    exists = path.exists()
    indexed = 0
    if exists:
        try:
            with sqlite3.connect(path) as conn:
                indexed = int(conn.execute("select count(*) from chunks").fetchone()[0])
        except sqlite3.Error:
            indexed = 0
    db_mtime = path.stat().st_mtime if exists else None
    stale = not exists or indexed == 0 or (db_mtime is not None and source_mtime > db_mtime)
    reason = "current"
    freshness_detail = ""
    if not exists:
        reason = "missing"
    elif indexed == 0:
        reason = "empty"
    elif db_mtime is not None and source_mtime > db_mtime:
        reason = "stale"
    elif exists:
        freshness = check_index_freshness(root)
        freshness_detail = freshness.detail
        if not freshness.ok:
            stale = True
            reason = "hash_mismatch"
            if "legacy index schema" in freshness.detail:
                reason = "legacy_schema"
            elif "index unreadable" in freshness.detail:
                reason = "unreadable"
    return {
        "db_path": path.relative_to(root).as_posix(),
        "exists": exists,
        "indexed": indexed,
        "stale": stale,
        "reason": reason,
        "freshness_detail": freshness_detail,
    }


def newest_source_mtime(root: Path) -> float:
    newest = 0.0
    for path in iter_text_files(root):
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest


def start_session(
    root: Path,
    *,
    agent: str,
    rebuild_mode: str = "auto",
    dry_run: bool = False,
    strict: bool = False,
    query_text: str | None = None,
    limit: int = 5,
    context_budget_mode: str = "balanced",
) -> dict[str, Any]:
    db_existed_before = db_path(root).exists()
    before = index_status(root)
    should_rebuild = rebuild_mode == "always" or (rebuild_mode == "auto" and before["stale"])
    index_payload: dict[str, Any] = {
        "rebuilt": False,
        "dry_run": dry_run,
        "reason": before["reason"],
        "before": before,
    }
    if should_rebuild and not dry_run:
        rebuilt = rebuild(root)
        index_payload.update({"rebuilt": True, "result": rebuilt, "after": index_status(root)})
    elif should_rebuild:
        index_payload["would_rebuild"] = True
    else:
        index_payload["would_rebuild"] = False

    hook_payload = handle_hook(root, "SessionStart", {"agent": agent, "dry": dry_run})
    doctor_payload = as_payload(run_checks(root))
    payload: dict[str, Any] = {
        "ok": bool(doctor_payload.get("ok")) if strict else bool(hook_payload.get("ok")),
        "agent": agent,
        "context_budget": context_budget_policy(context_budget_mode),
        "index": index_payload,
        "hook": hook_payload,
        "doctor": doctor_payload,
    }
    if query_text:
        payload["context"] = context_pack(root, query_text, limit=limit, mode=context_budget_mode)
    if not dry_run:
        try:
            from .session_resume import write_snapshot
            session_id = str(hook_payload.get("session_id") or "")
            if not session_id:
                from secrets import token_hex
                session_id = f"{agent}-{token_hex(6)}"
            snapshot = write_snapshot(root, session_id=session_id, agent=agent, context_budget_mode=context_budget_mode)
            payload["resume"] = {"ok": True, "path": snapshot.get("path"), "session_id": session_id}
        except Exception as exc:
            payload["resume"] = {"ok": False, "reason": str(exc)[:200]}
    elif not db_existed_before:
        # A dry-run session is a read-only preview. Some diagnostics may open
        # SQLite defensively; remove an empty just-created index so CI dry-runs
        # do not leave persistent cache state behind.
        for suffix in ("", "-wal", "-shm"):
            try:
                db_path(root).with_name(db_path(root).name + suffix).unlink(missing_ok=True)
            except OSError:
                pass
    return payload
