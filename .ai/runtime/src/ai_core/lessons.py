from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from .memory import append_audit, append_jsonl, now_iso, read_jsonl_all
from .redact import redact_value


def lessons_path(root: Path) -> Path:
    return root / ".ai" / "memory" / "lessons.jsonl"


def _short_id() -> str:
    import secrets

    return f"lesson-{secrets.token_hex(4)}"


def _clean_text(value: str, *, max_len: int) -> str:
    return str(redact_value(value)).strip()[:max_len]


def _clean_tags(tags: list[str] | None) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for tag in tags or []:
        clean = str(redact_value(tag)).strip()[:64]
        if not clean or clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
    return out


def add_lesson(
    root: Path,
    *,
    source: str,
    failure: str,
    cause: str,
    fix: str,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    record = {
        "id": _short_id(),
        "source": _clean_text(source or "operator", max_len=128),
        "failure": _clean_text(failure, max_len=1024),
        "cause": _clean_text(cause, max_len=1024),
        "fix": _clean_text(fix, max_len=1024),
        "tags": _clean_tags(tags),
        "created_at": now_iso(),
    }
    if not record["failure"] or not record["cause"] or not record["fix"]:
        return {"ok": False, "reason": "missing_required_field"}
    append_jsonl(lessons_path(root), record)
    append_audit(root, action="lessons.add", category="memory", payload={"id": record["id"], "source": record["source"]})
    return {"ok": True, "record": record}


def list_lessons(root: Path, *, limit: int = 20) -> dict[str, Any]:
    effective_limit = max(0, int(limit))
    records = read_jsonl_all(lessons_path(root))
    items = list(reversed(records))[:effective_limit]
    return {"ok": True, "count": len(items), "items": items}


def lesson_summary(root: Path) -> dict[str, Any]:
    records = read_jsonl_all(lessons_path(root))
    by_source: Counter[str] = Counter()
    by_tag: Counter[str] = Counter()
    for record in records:
        source = str(record.get("source") or "unknown")
        by_source[source] += 1
        for tag in record.get("tags") or []:
            by_tag[str(tag)] += 1
    return {
        "ok": True,
        "total": len(records),
        "by_source": dict(sorted(by_source.items())),
        "by_tag": dict(sorted(by_tag.items())),
    }
