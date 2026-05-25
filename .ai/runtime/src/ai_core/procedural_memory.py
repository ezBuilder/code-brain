from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from .memory import append_audit, append_jsonl, now_iso, read_jsonl_all
from .redact import redact_value

_PROCEDURAL_THREAD_LOCK = threading.RLock()


def procedural_path(root: Path) -> Path:
    """Return the procedural memory JSONL path: .ai/memory/procedural.jsonl"""
    return root / ".ai" / "memory" / "procedural.jsonl"


def _short_id() -> str:
    import secrets

    return f"proc-{secrets.token_hex(4)}"


def _clean_text(value: str, *, max_len: int = 2048) -> str:
    """Clean and redact text for storage."""
    return str(redact_value(value)).strip()[:max_len]


def _clean_tags(tags: list[str] | None) -> list[str]:
    """Deduplicate and clean tags."""
    seen: set[str] = set()
    out: list[str] = []
    for tag in tags or []:
        clean = str(redact_value(tag)).strip()[:64]
        if not clean or clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
    return out


def append_procedure(
    root: Path,
    *,
    kind: str,
    trigger: str,
    procedure: str,
    evidence: dict[str, Any] | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """
    Append a procedural memory record.

    Schema:
    {
      "id": "proc-...",
      "ts": iso8601,
      "kind": "lesson|skill_body|precall_rule|fix_pattern|...",
      "trigger": "<context key, e.g. pytest_failure>",
      "procedure": "<text body or short procedure description>",
      "evidence": {"source": "...", "id": "...", ...},
      "tags": ["..."],
    }

    Args:
        root: Project root
        kind: Category (e.g., "lesson", "skill_body", "precall_rule", "fix_pattern")
        trigger: Short context key (e.g., "pytest_failure", "import_error")
        procedure: Text body or procedure description
        evidence: Optional tracking dict (source, id, etc.)
        tags: Optional list of tags

    Returns:
        {"ok": True, "record": ...} on success
        {"ok": False, "reason": "..."} on silent fail
    """
    try:
        kind_clean = _clean_text(kind or "", max_len=64).lower()
        trigger_clean = _clean_text(trigger or "", max_len=128).lower()
        procedure_clean = _clean_text(procedure or "", max_len=2048)

        if not kind_clean or not trigger_clean or not procedure_clean:
            return {"ok": False, "reason": "missing_required_field"}

        record = {
            "id": _short_id(),
            "ts": now_iso(),
            "kind": kind_clean,
            "trigger": trigger_clean,
            "procedure": procedure_clean,
            "evidence": evidence or {},
            "tags": _clean_tags(tags),
        }
        append_jsonl(procedural_path(root), record)
        append_audit(
            root,
            action="procedural.append",
            category="memory",
            payload={"id": record["id"], "kind": kind_clean, "trigger": trigger_clean},
        )
        return {"ok": True, "record": record}
    except (OSError, ValueError, json.JSONDecodeError, Exception):
        return {"ok": False, "reason": "append_failed"}


def list_procedures(
    root: Path,
    *,
    limit: int = 20,
    kind: str | None = None,
    trigger: str | None = None,
) -> dict[str, Any]:
    """
    List procedural memory records (latest-first).

    Args:
        root: Project root
        limit: Max records to return
        kind: Optional filter by kind
        trigger: Optional filter by trigger

    Returns:
        {"ok": True, "count": ..., "items": [...]}
    """
    try:
        effective_limit = max(0, int(limit))
        records = read_jsonl_all(procedural_path(root))

        kind_filter = (kind or "").strip().lower() if kind else None
        trigger_filter = (trigger or "").strip().lower() if trigger else None

        filtered: list[dict[str, Any]] = []
        for rec in records:
            if kind_filter and str(rec.get("kind", "")).lower() != kind_filter:
                continue
            if trigger_filter and str(rec.get("trigger", "")).lower() != trigger_filter:
                continue
            filtered.append(rec)

        items = list(reversed(filtered))[:effective_limit]
        return {"ok": True, "count": len(items), "items": items}
    except (OSError, ValueError, Exception):
        return {"ok": True, "count": 0, "items": []}


def search_procedures(
    root: Path,
    *,
    query: str,
    limit: int = 10,
) -> dict[str, Any]:
    """
    Search procedural memory by substring or token match.

    Simple in-memory token-based search (no BM25 dependency).
    Matches query tokens against procedure, trigger, and tags.

    Args:
        root: Project root
        query: Search query (space-separated tokens)
        limit: Max results to return

    Returns:
        {"ok": True, "count": ..., "items": [...]}
    """
    try:
        effective_limit = max(0, int(limit))
        records = read_jsonl_all(procedural_path(root))

        query_lower = query.strip().lower()
        if not query_lower:
            return {"ok": True, "count": 0, "items": []}

        # Token-based matching: split query, score each record
        tokens = query_lower.split()
        results: list[tuple[float, dict[str, Any]]] = []

        for rec in records:
            score = 0.0

            # Check procedure text (highest weight)
            procedure_text = str(rec.get("procedure", "")).lower()
            for token in tokens:
                if token in procedure_text:
                    score += 2.0
                    if procedure_text.startswith(token):
                        score += 1.0

            # Check trigger
            trigger_text = str(rec.get("trigger", "")).lower()
            for token in tokens:
                if token in trigger_text:
                    score += 1.5

            # Check kind
            kind_text = str(rec.get("kind", "")).lower()
            for token in tokens:
                if token in kind_text:
                    score += 1.0

            # Check tags
            tags = rec.get("tags") or []
            tag_text = " ".join(str(t).lower() for t in tags)
            for token in tokens:
                if token in tag_text:
                    score += 0.5

            if score > 0:
                results.append((score, rec))

        # Sort by score (descending)
        results.sort(key=lambda x: -x[0])
        items = [rec for _, rec in results[:effective_limit]]

        return {"ok": True, "count": len(items), "items": items}
    except (OSError, ValueError, Exception):
        return {"ok": True, "count": 0, "items": []}


def consolidate_from_lessons(
    root: Path,
    *,
    since_ts: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Migrate learned patterns from lessons.jsonl to procedural.jsonl.

    Deduplicates by (kind, trigger) — only the most recent record is kept.
    Lesions are converted to procedural entries with:
    - kind: "lesson"
    - trigger: lessons source or "failure_pattern"
    - procedure: combined failure + cause + fix
    - evidence: points back to lesson source and id

    Args:
        root: Project root
        since_ts: Optional ISO8601 timestamp to process only newer lessons
        dry_run: If True, return what would be merged without writing

    Returns:
        {
            "ok": True,
            "merged": int,
            "deduplicated": int,
            "preview": [...] (if dry_run),
        }
    """
    try:
        from .lessons import lessons_path

        lessons = read_jsonl_all(lessons_path(root))
        since = (since_ts or "").strip().lower() if since_ts else None

        # Filter by timestamp if provided
        if since:
            filtered = []
            for lesson in lessons:
                ts = str(lesson.get("created_at") or lesson.get("ts") or "").lower()
                if ts >= since:
                    filtered.append(lesson)
            lessons = filtered

        # Dedup by (source, trigger_key) — keep only latest
        by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for lesson in lessons:
            source = str(lesson.get("source") or "unknown")
            # Use source or failure keyword as trigger
            trigger_key = source if source != "unknown" else "failure_pattern"

            key = (source, trigger_key)
            by_key[key] = lesson

        procedures_to_add: list[dict[str, Any]] = []
        for (source, trigger_key), lesson in by_key.items():
            failure = str(lesson.get("failure") or "").strip()
            cause = str(lesson.get("cause") or "").strip()
            fix = str(lesson.get("fix") or "").strip()

            procedure_text = f"Failure: {failure}\nCause: {cause}\nFix: {fix}"
            evidence = {
                "source": "lessons",
                "lesson_source": source,
                "lesson_id": lesson.get("id"),
            }
            tags = lesson.get("tags") or []

            procedures_to_add.append(
                {
                    "kind": "lesson",
                    "trigger": trigger_key,
                    "procedure": procedure_text,
                    "evidence": evidence,
                    "tags": tags,
                }
            )

        if dry_run:
            return {
                "ok": True,
                "merged": len(procedures_to_add),
                "deduplicated": len(lessons) - len(procedures_to_add),
                "preview": procedures_to_add[:5],
            }

        # Write procedures
        merged_count = 0
        for proc_data in procedures_to_add:
            result = append_procedure(root, **proc_data)
            if result.get("ok"):
                merged_count += 1

        return {
            "ok": True,
            "merged": merged_count,
            "deduplicated": len(lessons) - merged_count,
        }
    except (OSError, ValueError, json.JSONDecodeError, Exception) as e:
        return {
            "ok": False,
            "reason": f"consolidate_failed: {type(e).__name__}",
            "merged": 0,
            "deduplicated": 0,
        }
