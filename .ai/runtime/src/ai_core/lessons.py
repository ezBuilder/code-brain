from __future__ import annotations

import json
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


def append_lesson(
    root: Path,
    *,
    kind: str,
    command: str,
    outcome: str,
    details: str = "",
) -> dict[str, Any]:
    """
    Append a lesson from an eval_loop failure.

    Schema: {"ts": iso8601, "source": "eval_fail", "kind": ..., "command": ..., "outcome": ..., "details": ...}

    Args:
        root: Project root
        kind: Evaluation kind (e.g., "swe", "cli")
        command: Command that failed
        outcome: Non-pass outcome string
        details: Optional additional context (e.g., "duration_ms=123")

    Returns:
        {"ok": True, "record": ...} on success
        {"ok": False, "reason": "..."} on silent fail
    """
    try:
        record = {
            "ts": now_iso(),
            "source": "eval_fail",
            "kind": _clean_text(kind, max_len=128),
            "command": _clean_text(command, max_len=512),
            "outcome": _clean_text(outcome, max_len=128),
            "details": _clean_text(details, max_len=256) if details else "",
        }
        if not record["kind"] or not record["command"] or not record["outcome"]:
            return {"ok": False, "reason": "missing_required_field"}
        append_jsonl(lessons_path(root), record)
        append_audit(root, action="lessons.append", category="memory", payload={"source": "eval_fail", "kind": record["kind"]})

        # Auto-trigger consolidate_from_lessons when append count reaches N=5 modulo
        _maybe_trigger_consolidate(root)

        return {"ok": True, "record": record}
    except (OSError, ValueError, json.JSONDecodeError, Exception):
        return {"ok": False, "reason": "append_failed"}


def _maybe_trigger_consolidate(root: Path, interval: int = 5) -> None:
    """Fire consolidate_from_lessons every N appends (modulo check on line count)."""
    try:
        lp = lessons_path(root)
        if not lp.exists():
            return
        # Count lines as cheap proxy for append count
        count = sum(1 for _ in lp.read_text(encoding="utf-8", errors="ignore").splitlines())
        if count > 0 and count % interval == 0:
            # Consolidate asynchronously (fire-and-forget)
            _spawn_consolidate_background(root)
    except Exception:
        pass


def _spawn_consolidate_background(root: Path) -> None:
    """Fire-and-forget background consolidate_from_lessons."""
    import os
    import subprocess
    import sys

    try:
        from .portable import detached_popen_kwargs
        from .process_janitor import cleanup_children, register_child

        cleanup_children(root)

        lock_path = root / ".ai" / "cache" / "consolidate.lock"
        import time
        try:
            if lock_path.exists() and time.time() - lock_path.stat().st_mtime < 60:
                return
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            return
        except OSError:
            pass

        cmd = [
            sys.executable, "-c",
            "from ai_core.procedural_memory import consolidate_from_lessons; "
            "from pathlib import Path; "
            f"r=Path({str(root)!r}); lock=Path({str(lock_path)!r}); "
            "\ntry:\n    consolidate_from_lessons(r, dry_run=False)\nfinally:\n    lock.unlink(missing_ok=True)",
        ]
        env = {**os.environ, "PYTHONPATH": str(root / ".ai" / "runtime" / "src")}
        with open(os.devnull, "wb") as devnull:
            proc = subprocess.Popen(
                cmd, stdout=devnull, stderr=devnull, stdin=subprocess.DEVNULL,
                env=env, **detached_popen_kwargs(),
            )
        register_child(root, pid=proc.pid, kind="consolidate_lessons", command=cmd)
    except Exception:
        pass
