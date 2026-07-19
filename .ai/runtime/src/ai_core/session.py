from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .doctor import as_payload, run_checks
from .hooks import handle_hook
from .context_budget import policy as context_budget_policy
from .memory import rebuild_audit_index
from .policy import is_ci
from .render import render as render_project
from .search import context_pack, db_path, index_hash_status, rebuild


def index_status(
    root: Path,
    *,
    use_metadata: bool = False,
    refresh_metadata: bool = False,
) -> dict[str, Any]:
    path = db_path(root)
    exists = path.exists()
    indexed = 0
    if exists:
        try:
            with sqlite3.connect(path) as conn:
                indexed = int(conn.execute("select count(*) from chunks").fetchone()[0])
        except sqlite3.Error:
            indexed = 0
    stale = not exists or indexed == 0
    reason = "current"
    freshness_detail = ""
    changed_paths: list[str] = []
    if not exists:
        reason = "missing"
    elif indexed == 0:
        reason = "empty"
    elif exists:
        freshness = index_hash_status(
            root,
            use_metadata=use_metadata,
            refresh_metadata=refresh_metadata,
            use_candidate_cache=use_metadata,
        )
        changed_paths = list(freshness.get("changed_paths") or [])
        freshness_detail = str(freshness.get("detail") or "")
        if changed_paths:
            freshness_detail = "stale: " + ", ".join(changed_paths[:10])
        if not freshness.get("ok"):
            stale = True
            reason = str(freshness.get("reason") or "hash_mismatch")
    return {
        "db_path": path.relative_to(root).as_posix(),
        "exists": exists,
        "indexed": indexed,
        "stale": stale,
        "reason": reason,
        "freshness_detail": freshness_detail,
        "changed_paths": changed_paths,
    }


def start_session(
    root: Path,
    *,
    agent: str,
    rebuild_mode: str = "auto",
    dry_run: bool = False,
    strict: bool = False,
    repair_audit_index: bool = False,
    render_manifest: bool = False,
    query_text: str | None = None,
    limit: int = 5,
    context_budget_mode: str = "balanced",
) -> dict[str, Any]:
    db_existed_before = db_path(root).exists()
    before = index_status(
        root,
        use_metadata=not strict,
        refresh_metadata=not strict and not is_ci(),
    )
    should_rebuild = rebuild_mode == "always" or (rebuild_mode == "auto" and before["stale"])
    index_payload: dict[str, Any] = {
        "rebuilt": False,
        "dry_run": dry_run,
        "reason": before["reason"],
        "before": before,
    }
    if should_rebuild and not dry_run:
        rebuilt = rebuild(root)
        index_payload.update(
            {
                "rebuilt": True,
                "result": rebuilt,
                "after": index_status(
                    root,
                    use_metadata=not strict,
                    refresh_metadata=not strict and not is_ci(),
                ),
            }
        )
    elif should_rebuild:
        index_payload["would_rebuild"] = True
    else:
        index_payload["would_rebuild"] = False

    audit_index_payload: dict[str, Any] | None = None
    if repair_audit_index:
        if dry_run:
            audit_index_payload = {"repaired": False, "would_repair": True, "dry_run": True}
        else:
            audit_index_payload = {"repaired": True, **rebuild_audit_index(root)}

    render_payload: dict[str, Any] | None = None
    if render_manifest:
        render_payload = render_project(root, dry_run=dry_run, manifest_only=True)

    hook_payload = handle_hook(root, "SessionStart", {"agent": agent, "dry": dry_run})
    verified_index_status = index_payload.get("after") if index_payload.get("rebuilt") else before
    hook_elapsed = hook_payload.get("elapsed_ms")
    verified_session_start_ms = (
        max(0, int(hook_elapsed))
        if isinstance(hook_elapsed, (int, float)) and not isinstance(hook_elapsed, bool)
        else None
    )
    doctor_payload = as_payload(
        run_checks(
            root,
            precomputed_index_status=verified_index_status,
            precomputed_session_start_ms=verified_session_start_ms,
            lightweight=not strict,
            update_scan_state=not dry_run and not is_ci(),
        )
    )
    payload: dict[str, Any] = {
        "ok": bool(doctor_payload.get("ok")) if strict else bool(hook_payload.get("ok")),
        "agent": agent,
        "context_budget": context_budget_policy(context_budget_mode),
        "index": index_payload,
        "hook": hook_payload,
        "doctor": doctor_payload,
    }
    if audit_index_payload is not None:
        payload["audit_index"] = audit_index_payload
    if render_payload is not None:
        payload["render_manifest"] = render_payload
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
