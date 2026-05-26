from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
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


# --- Confidence + decay scoring (read-time, append-only safe) ----------------
# Lessons are append-only, so repeated observations of the "same" lesson are
# reinforcement signals rather than mutations. Confidence is rebuilt at read
# time: each occurrence strengthens it; elapsed weeks since the last sighting
# decay it. Pure-local, no network. Adapted from agentmemory's lesson model.


def _parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            return datetime.fromisoformat(value[:-1]).replace(tzinfo=timezone.utc)
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def lesson_fingerprint(record: dict[str, Any]) -> str:
    """Stable identity for reinforcement/dedup across append-only occurrences."""
    failure = str(record.get("failure") or "").strip().lower()
    cause = str(record.get("cause") or "").strip().lower()
    fix = str(record.get("fix") or "").strip().lower()
    if failure or cause or fix:
        key = f"{str(record.get('source') or '').lower()}|{failure}|{cause}|{fix}"
    else:  # eval_fail shape
        key = "|".join(
            str(record.get(k) or "").strip().lower()
            for k in ("source", "kind", "command", "outcome")
        )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _env_float(name: str, default: float) -> float:
    import os

    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    return default if val != val else val


def score_lessons(
    root: Path,
    *,
    now: datetime | None = None,
    decay_rate: float | None = None,
    include_stale: bool = True,
) -> dict[str, Any]:
    """Group lessons by fingerprint and assign a decayed confidence.

      confidence starts 0.5, reinforced once per repeat: c <- c + 0.1*(1-c)
      decayed by elapsed weeks since last sighting: c <- c - decay_rate*weeks
      floored at 0.05; `stale` when c <= 0.1 and seen only once.
    """
    rate = _env_float("AI_LESSON_DECAY_RATE", 0.05) if decay_rate is None else float(decay_rate)
    moment = now or datetime.now(timezone.utc)

    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for rec in read_jsonl_all(lessons_path(root)):
        fp = lesson_fingerprint(rec)
        if fp not in groups:
            groups[fp] = []
            order.append(fp)
        groups[fp].append(rec)

    items: list[dict[str, Any]] = []
    for fp in order:
        group = groups[fp]
        reinforcements = len(group)
        confidence = 0.5
        for _ in range(reinforcements - 1):
            confidence = min(1.0, confidence + 0.1 * (1.0 - confidence))

        last_dt: datetime | None = None
        for rec in group:
            dt = _parse_ts(str(rec.get("created_at") or rec.get("ts") or ""))
            if dt is not None and (last_dt is None or dt > last_dt):
                last_dt = dt
        weeks = 0.0
        if last_dt is not None:
            weeks = max(0.0, (moment - last_dt).total_seconds() / (7 * 86400.0))
        confidence = max(0.05, confidence - rate * weeks)

        stale = confidence <= 0.1 and reinforcements <= 1
        if stale and not include_stale:
            continue

        latest = dict(group[-1])
        latest.update({
            "fingerprint": fp,
            "confidence": round(confidence, 4),
            "reinforcements": reinforcements,
            "stale": stale,
            "last_seen": last_dt.isoformat() if last_dt else None,
        })
        items.append(latest)

    items.sort(key=lambda r: r["confidence"], reverse=True)
    return {"ok": True, "count": len(items), "items": items}


def recall_lessons(
    root: Path,
    *,
    query: str,
    limit: int = 10,
    include_stale: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Rank lessons for a query: confidence * relevance * recency.

    relevance = matched query tokens / total tokens (over failure+cause+fix+
    tags+command). recency = 1/(1 + days_since_last_seen*0.01). Pure-local.
    """
    tokens = [t for t in str(query or "").strip().lower().split() if t]
    if not tokens:
        return {"ok": True, "count": 0, "items": []}

    moment = now or datetime.now(timezone.utc)
    scored = score_lessons(root, now=moment, include_stale=include_stale).get("items", [])

    results: list[dict[str, Any]] = []
    for item in scored:
        haystack = " ".join(
            str(item.get(k) or "")
            for k in ("failure", "cause", "fix", "kind", "command", "outcome")
        ).lower()
        haystack += " " + " ".join(str(t).lower() for t in (item.get("tags") or []))
        hits = sum(1 for t in tokens if t in haystack)
        if hits == 0:
            continue
        relevance = hits / len(tokens)

        days = 0.0
        last_dt = _parse_ts(str(item.get("last_seen") or ""))
        if last_dt is not None:
            days = max(0.0, (moment - last_dt).total_seconds() / 86400.0)
        recency = 1.0 / (1.0 + days * 0.01)

        score = float(item.get("confidence", 0.5)) * relevance * recency
        out = dict(item)
        out["recall_score"] = round(score, 6)
        out["relevance"] = round(relevance, 4)
        results.append(out)

    results.sort(key=lambda r: r["recall_score"], reverse=True)
    return {"ok": True, "count": len(results[: max(0, int(limit))]), "items": results[: max(0, int(limit))]}


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
