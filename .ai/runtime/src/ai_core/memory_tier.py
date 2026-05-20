"""MemGPT-inspired virtual memory tiering for CodeBrain (T30 step A).

Maps the project's persisted memory into three explicit tiers, modeled on
OS virtual-memory paging (MemGPT) + biological-memory consolidation:

  HOT  — recent, low-latency, small footprint (∈ "main context")
         · audit events younger than HOT_TTL_HOURS (default 1h)
         · open todos
         · session-current.md tail (last SESSION_HOT_LINES lines)

  WARM — medium-term, on-disk per-year files
         · audit events HOT_TTL..WARM_TTL_DAYS (default 7d)
         · recent decisions

  COLD — long-term archive, opt-in load
         · audit events older than WARM_TTL_DAYS
         · closed todos
         · prior session resume snapshots

This module is read-only. Page-in/page-out lands in steps B/C.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    if val != val:  # NaN
        return default
    return val


def hot_ttl_hours() -> float:
    return max(0.0, _env_float("AI_MEMORY_HOT_TTL_HOURS", 1.0))


def warm_ttl_days() -> float:
    return max(0.0, _env_float("AI_MEMORY_WARM_TTL_DAYS", 7.0))


def _parse_ts(s: str) -> datetime | None:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            return datetime.fromisoformat(s[:-1]).replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def classify(root: Path) -> dict[str, Any]:
    """Summarize the memory store as a 3-tier histogram + cohort sizes.

    Pure read; never writes. Output schema is additive — callers (CLI, MCP,
    SessionStart context) treat unknown keys as informational.
    """
    from .memory import all_audit_files, todos_path, decisions_path, session_current_path

    now = datetime.now(timezone.utc)
    hot_cutoff = now - timedelta(hours=hot_ttl_hours())
    warm_cutoff = now - timedelta(days=warm_ttl_days())

    audit_files = all_audit_files(root)
    audit_total = 0
    audit_hot = 0
    audit_warm = 0
    audit_cold = 0
    audit_bytes = 0
    for af in audit_files:
        try:
            audit_bytes += af.stat().st_size
            content = af.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            audit_total += 1
            ts = _parse_ts(str(rec.get("ts") or ""))
            if ts is None:
                audit_cold += 1
                continue
            if ts >= hot_cutoff:
                audit_hot += 1
            elif ts >= warm_cutoff:
                audit_warm += 1
            else:
                audit_cold += 1

    todos_open = 0
    todos_closed = 0
    tpath = todos_path(root)
    if tpath.exists():
        try:
            # latest status per id
            latest: dict[str, str] = {}
            for line in tpath.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                tid = str(rec.get("id") or "")
                if not tid:
                    continue
                latest[tid] = str(rec.get("status") or "open").lower()
            for status in latest.values():
                if status in {"done", "closed", "completed", "cancelled", "canceled"}:
                    todos_closed += 1
                else:
                    todos_open += 1
        except OSError:
            pass

    decisions_count = 0
    dpath = decisions_path(root)
    if dpath.exists():
        try:
            decisions_count = sum(1 for line in dpath.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip())
        except OSError:
            pass

    session_path = session_current_path(root)
    session_bytes = session_path.stat().st_size if session_path.exists() else 0

    sessions_dir = root / ".ai" / "memory" / "sessions"
    archived_sessions = 0
    if sessions_dir.is_dir():
        archived_sessions = sum(1 for _ in sessions_dir.iterdir() if _.is_dir())

    return {
        "ok": True,
        "tiers": {
            "hot": {
                "audit_events": audit_hot,
                "todos_open": todos_open,
                "session_bytes": session_bytes,
                "ttl_hours": hot_ttl_hours(),
            },
            "warm": {
                "audit_events": audit_warm,
                "decisions": decisions_count,
                "ttl_days": warm_ttl_days(),
            },
            "cold": {
                "audit_events": audit_cold,
                "todos_closed": todos_closed,
                "archived_sessions": archived_sessions,
            },
        },
        "totals": {
            "audit_events": audit_total,
            "audit_bytes": audit_bytes,
            "audit_files": len(audit_files),
        },
    }


def archive_old_sessions(root: Path, *, age_days: float = 30.0, dry_run: bool = False) -> dict[str, Any]:
    """Move per-session snapshot directories older than `age_days` to
    .ai/memory/sessions/.archive/ — page-out for the cold tier.
    Safe: never deletes anything; only mtime-based rename. Returns a manifest.
    """
    import shutil
    import time

    sessions_dir = root / ".ai" / "memory" / "sessions"
    if not sessions_dir.is_dir():
        return {"ok": True, "moved": [], "kept": [], "archive_dir": None, "dry_run": dry_run}

    archive_dir = sessions_dir / ".archive"
    cutoff = time.time() - age_days * 86400.0
    moved: list[str] = []
    kept: list[str] = []

    for child in sorted(sessions_dir.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            kept.append(child.name)
            continue
        try:
            mt = child.stat().st_mtime
        except OSError:
            continue
        if mt >= cutoff:
            kept.append(child.name)
            continue
        if dry_run:
            moved.append(child.name)
            continue
        archive_dir.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(child), str(archive_dir / child.name))
            moved.append(child.name)
        except (OSError, shutil.Error):
            kept.append(child.name)

    return {
        "ok": True,
        "moved": moved,
        "kept": kept,
        "archive_dir": str(archive_dir.relative_to(root)) if archive_dir.exists() else None,
        "age_days": age_days,
        "dry_run": dry_run,
    }


def page_out(root: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """High-level page-out: rotate session-current if pressured + archive
    sessions older than AI_MEMORY_ARCHIVE_DAYS (default 30)."""
    from .memory import (
        _SESSION_NOTE_MAX_BYTES, _SESSION_NOTE_KEEP_BYTES, session_current_path,
    )

    age_days = _env_float("AI_MEMORY_ARCHIVE_DAYS", 30.0)
    result: dict[str, Any] = {"ok": True, "dry_run": dry_run}

    spath = session_current_path(root)
    rotated = False
    if spath.exists():
        size = spath.stat().st_size
        if size >= int(_SESSION_NOTE_MAX_BYTES * 0.8):
            if dry_run:
                rotated = True
            else:
                try:
                    raw = spath.read_bytes()
                    tail = raw[-_SESSION_NOTE_KEEP_BYTES:]
                    nl = tail.find(b"\n")
                    if nl >= 0:
                        tail = tail[nl + 1:]
                    spath.write_bytes(b"# Current Session\n\n[rotated by page_out]\n" + tail)
                    rotated = True
                except OSError:
                    pass
    result["session_rotated"] = rotated
    result["session_size_after"] = spath.stat().st_size if spath.exists() else 0

    arch = archive_old_sessions(root, age_days=age_days, dry_run=dry_run)
    result["archived"] = arch
    return result


def hot_pressure(root: Path) -> dict[str, Any]:
    """Quick health summary — is the hot tier approaching its limits?

    Returns ratio of session-current.md size to the 100KB rotation cap and
    a flag when hot audit events exceed a sensible budget.
    """
    from .memory import _SESSION_NOTE_MAX_BYTES, session_current_path

    spath = session_current_path(root)
    session_bytes = spath.stat().st_size if spath.exists() else 0
    session_ratio = session_bytes / float(_SESSION_NOTE_MAX_BYTES) if _SESSION_NOTE_MAX_BYTES else 0.0

    classification = classify(root)
    hot_events = classification["tiers"]["hot"]["audit_events"]
    audit_pressure = hot_events / 1000.0  # >1.0 means we're over a soft budget

    return {
        "ok": True,
        "session_md_ratio": round(session_ratio, 4),
        "session_md_bytes": session_bytes,
        "session_md_cap": _SESSION_NOTE_MAX_BYTES,
        "audit_pressure_ratio": round(audit_pressure, 4),
        "hot_audit_events": hot_events,
        "page_out_recommended": session_ratio >= 0.8 or audit_pressure >= 1.0,
    }
