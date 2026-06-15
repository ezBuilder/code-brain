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
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Retention salience baselines per memory type (adapted from agentmemory's
# retention model). Higher = more durable. Pure-local, no network.
RETENTION_TYPE_WEIGHTS: dict[str, float] = {
    "decision": 0.9,
    "architecture": 0.9,
    "preference": 0.85,
    "pattern": 0.8,
    "skill": 0.8,
    "bug": 0.7,
    "lesson": 0.7,
    "precall": 0.65,
    "workflow": 0.6,
    "procedure": 0.6,
    "fact": 0.5,
    "todo": 0.5,
    "event": 0.4,
}
_RETENTION_DEFAULT_WEIGHT = 0.5


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
        except OSError:
            continue
        try:
            with af.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
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
        except OSError:
            continue

    todos_open = 0
    todos_closed = 0
    tpath = todos_path(root)
    if tpath.exists():
        try:
            # latest status per id
            latest: dict[str, str] = {}
            with tpath.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
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
            with dpath.open(encoding="utf-8", errors="replace") as fh:
                decisions_count = sum(1 for line in fh if line.strip())
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
    sessions older than AI_MEMORY_ARCHIVE_DAYS (default 30) + fold audit
    entries older than AI_AUDIT_FOLD_DAYS (default 30)."""
    from .memory import (
        _SESSION_NOTE_MAX_BYTES, _SESSION_NOTE_KEEP_BYTES, EVENTS_KEEP, EVENTS_MAX_BYTES,
        events_path, rotate_jsonl_tail, session_current_path,
    )
    from .audit_fold import fold_old_entries

    age_days = _env_float("AI_MEMORY_ARCHIVE_DAYS", 30.0)
    audit_fold_days = int(_env_float("AI_AUDIT_FOLD_DAYS", 30.0))
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

    volatile: dict[str, Any] = {
        "events": rotate_jsonl_tail(
            events_path(root),
            max_bytes=EVENTS_MAX_BYTES,
            keep_lines=EVENTS_KEEP,
            dry_run=dry_run,
        )
    }
    try:
        from . import prompt_growth
        volatile["prompt_growth"] = prompt_growth.rotate_logs(root, dry_run=dry_run)
    except Exception as exc:
        volatile["prompt_growth"] = {"ok": False, "error": str(exc)}
    try:
        from . import evidence
        volatile["evidence"] = evidence.rotate_ledger(root, dry_run=dry_run)
    except Exception as exc:
        volatile["evidence"] = {"ok": False, "error": str(exc)}
    result["volatile_logs"] = volatile

    # Fold old audit entries (separated, never blocks page_out)
    if audit_fold_days <= 0:
        result["audit_fold"] = {
            "ok": True,
            "skipped": True,
            "reason": "disabled (AI_AUDIT_FOLD_DAYS <= 0)",
        }
    else:
        try:
            fold_result = fold_old_entries(root, days=audit_fold_days, dry_run=dry_run)
            result["audit_fold"] = fold_result
        except Exception as e:
            # Non-fatal: log but keep page_out ok=True
            result["audit_fold"] = {
                "ok": False,
                "error": str(e),
            }

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


# --- Retention scoring (decay + reinforcement) -------------------------------
# A score-based view of memory durability, complementing the TTL-bucket
# classify(). Fully deterministic and local: exponential time decay, optional
# access-reinforcement, and per-type salience. No network, hot-path safe.


def _decay_lambda() -> float:
    return max(0.0, _env_float("AI_MEMORY_DECAY_LAMBDA", 0.01))


def _reinforce_sigma() -> float:
    return max(0.0, _env_float("AI_MEMORY_REINFORCE_SIGMA", 0.3))


def retention_score(
    *,
    mem_type: str,
    age_days: float,
    access_count: int = 0,
    recent_access_days: list[float] | None = None,
    confidence: float | None = None,
) -> float:
    """Durability score in [0, 1] for a single memory item.

      salience      = type_weight + min(0.2, access_count*0.02)
                      (raised to `confidence` when one is supplied)
      temporal      = exp(-lambda * age_days)        lambda=AI_MEMORY_DECAY_LAMBDA
      reinforcement = sigma * sum(1/days_since_access)  sigma=AI_MEMORY_REINFORCE_SIGMA
      score         = min(1, salience*temporal + reinforcement)
    """
    base = RETENTION_TYPE_WEIGHTS.get(str(mem_type or "").lower(), _RETENTION_DEFAULT_WEIGHT)
    salience = base + min(0.2, max(0, int(access_count)) * 0.02)
    if confidence is not None:
        try:
            salience = max(salience, float(confidence))
        except (TypeError, ValueError):
            pass
    salience = min(1.0, salience)

    temporal = math.exp(-_decay_lambda() * max(0.0, float(age_days)))

    boost = 0.0
    if recent_access_days:
        sigma = _reinforce_sigma()
        boost = sigma * sum(1.0 / max(1.0, float(d)) for d in recent_access_days)

    return max(0.0, min(1.0, salience * temporal + boost))


def score_tier(score: float) -> str:
    """Map a retention score to hot/warm/cold/evictable (env-overridable cuts)."""
    hot = _env_float("AI_MEMORY_TIER_HOT", 0.7)
    warm = _env_float("AI_MEMORY_TIER_WARM", 0.4)
    cold = _env_float("AI_MEMORY_TIER_COLD", 0.15)
    if score >= hot:
        return "hot"
    if score >= warm:
        return "warm"
    if score >= cold:
        return "cold"
    return "evictable"


def _ts_of(rec: dict[str, Any]) -> datetime | None:
    for key in ("ts", "created_at", "decided_at", "updated_at", "last_seen"):
        dt = _parse_ts(str(rec.get(key) or ""))
        if dt is not None:
            return dt
    return None


def retention_report(root: Path, *, evict_limit: int = 50) -> dict[str, Any]:
    """Score durable memory items (decisions, lessons, procedures) by retention.

    Read-only: returns a tier histogram plus the lowest-scoring items as
    eviction *candidates* (recommendation only — never deletes). Lessons fold
    in their confidence/reinforcement signal from `lessons.score_lessons`.
    """
    from .memory import decisions_path, read_jsonl_all
    from .lessons import lessons_path, score_lessons
    from .procedural_memory import procedural_path

    now = datetime.now(timezone.utc)

    def _age_days(rec: dict[str, Any]) -> float:
        dt = _ts_of(rec)
        if dt is None:
            return 365.0  # unknown age → treat as stale
        return max(0.0, (now - dt).total_seconds() / 86400.0)

    scored: list[dict[str, Any]] = []

    # Fold by id so a superseded failure (reused-id reappend) is scored once, not N times.
    _decisions_by_id: dict[str, dict[str, Any]] = {}
    _decisions_order: list[str] = []
    for rec in read_jsonl_all(decisions_path(root)):
        if not isinstance(rec, dict):
            continue
        rid = str(rec.get("id") or f"_anon{len(_decisions_order)}")
        if rid not in _decisions_by_id:
            _decisions_order.append(rid)
        _decisions_by_id[rid] = rec
    for rid in _decisions_order:
        rec = _decisions_by_id[rid]
        s = retention_score(mem_type="decision", age_days=_age_days(rec))
        scored.append({
            "kind": "decision",
            "score": round(s, 4),
            "tier": score_tier(s),
            "age_days": round(_age_days(rec), 2),
            "ref": str(rec.get("id") or rec.get("decision", ""))[:80],
        })

    # Lessons: use confidence + reinforcement count as salience/access signal.
    lesson_scores = score_lessons(root, now=now, include_stale=True).get("items", [])
    for item in lesson_scores:
        conf = item.get("confidence")
        reinf = int(item.get("reinforcements", 1) or 1)
        s = retention_score(
            mem_type="lesson",
            age_days=_age_days(item),
            access_count=reinf,
            confidence=conf,
        )
        scored.append({
            "kind": "lesson",
            "score": round(s, 4),
            "tier": score_tier(s),
            "age_days": round(_age_days(item), 2),
            "confidence": conf,
            "reinforcements": reinf,
            "ref": str(item.get("id") or item.get("failure", ""))[:80],
        })

    for rec in read_jsonl_all(procedural_path(root)):
        mtype = str(rec.get("kind") or "procedure").lower()
        s = retention_score(mem_type=mtype, age_days=_age_days(rec))
        scored.append({
            "kind": "procedure",
            "score": round(s, 4),
            "tier": score_tier(s),
            "age_days": round(_age_days(rec), 2),
            "ref": str(rec.get("id") or rec.get("trigger", ""))[:80],
        })

    hist = {"hot": 0, "warm": 0, "cold": 0, "evictable": 0}
    for item in scored:
        hist[item["tier"]] += 1

    evict_cap = max(0, int(evict_limit))
    candidates = sorted(
        (i for i in scored if i["tier"] == "evictable"),
        key=lambda i: i["score"],
    )[:evict_cap]

    return {
        "ok": True,
        "scored": len(scored),
        "tiers": hist,
        "decay_lambda": _decay_lambda(),
        "reinforce_sigma": _reinforce_sigma(),
        "evict_candidates": candidates,
    }
