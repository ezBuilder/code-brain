"""
Fold cold audit entries (30+ days old) into daily summary records.

Reduces storage and memory peak by compressing historical audit log entries
into one fold record per day, preserving action counts and metadata.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .memory import (
    AUDIT_LINE_MAX_BYTES,
    AUDIT_MAX_BYTES,
    _chained_line,
    all_audit_files,
    jsonl_lock_path,
    rebuild_audit_index,
)
from .private_write import (
    atomic_write_private_lines,
    open_root_confined_binary,
    private_file_lock,
)


def _parse_ts(ts_str: str) -> datetime | None:
    """Parse ISO timestamp to UTC datetime, or None if invalid."""
    try:
        ts_str_clean = ts_str.replace("Z", "+00:00")
        return datetime.fromisoformat(ts_str_clean)
    except (ValueError, AttributeError, TypeError):
        return None


def _date_from_ts(ts: datetime) -> str:
    """Return YYYY-MM-DD string for a datetime."""
    return ts.date().isoformat()


def _iter_audit_rows(path: Path, *, root: Path) -> Iterator[tuple[str, dict[str, Any] | None]]:
    """Stream one bounded audit line at a time.

    Invalid JSON is returned as ``None`` so callers can preserve the original
    line. Invalid UTF-8 and oversized lines fail closed because they cannot be
    round-tripped safely through the UTF-8 atomic writer.
    """
    with open_root_confined_binary(
        path,
        root=root,
        max_bytes=AUDIT_MAX_BYTES,
        require_private=False,
    ) as (handle, _state):
        while True:
            raw = handle.readline(int(AUDIT_LINE_MAX_BYTES) + 1)
            if not raw:
                break
            if len(raw) > int(AUDIT_LINE_MAX_BYTES):
                while raw and not raw.endswith(b"\n"):
                    raw = handle.readline(64 * 1024)
                raise OSError(f"audit line exceeds {AUDIT_LINE_MAX_BYTES} bytes")
            try:
                line = raw.decode("utf-8", errors="strict").rstrip("\r\n")
            except UnicodeDecodeError as exc:
                raise OSError("audit line is not valid UTF-8") from exc
            if not line.strip():
                continue
            try:
                loaded = json.loads(line)
            except json.JSONDecodeError:
                yield line, None
                continue
            yield line, loaded if isinstance(loaded, dict) else None


def _cold_date(entry: dict[str, Any], *, cutoff: datetime) -> str | None:
    if entry.get("action") == "_folded":
        return None
    parsed = _parse_ts(entry.get("ts"))
    if parsed is None or parsed >= cutoff:
        return None
    return _date_from_ts(parsed)


def _fold_record(
    *,
    date_key: str,
    summary: dict[str, Any],
    source_path: str,
) -> dict[str, Any]:
    return {
        "action": "_folded",
        "category": "audit",
        "payload": {
            "date": date_key,
            "counts": dict(summary["counts"]),
            "total": int(summary["total"]),
            "source_files": [source_path],
        },
        "ts": f"{date_key}T23:59:59Z",
    }


def fold_old_entries(
    root: Path,
    *,
    days: int = 30,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Fold audit entries older than N days into daily summary records.

    Each day's folded entries become a single record:
        {
            "ts": "<date>T23:59:59Z",
            "action": "_folded",
            "payload": {
                "date": "YYYY-MM-DD",
                "counts": {"action_name": count, ...},
                "total": total_count,
                "source_files": [...]
            }
        }

    Args:
        root: Project root (contains .ai/memory/audit/).
        days: Entries older than this many days are folded. Default 30.
        dry_run: If True, report what would be folded but don't modify files.

    Returns:
        {
            "ok": True/False,
            "folded_days": int (number of dates folded),
            "removed_entries": int (original lines removed),
            "added_fold_records": int (new _folded records added),
            "files_touched": [str] (relative paths of modified files),
            "dry_run": bool,
            "errors": [str] (per-file error messages, if any),
        }
    """
    result: dict[str, Any] = {
        "ok": False,
        "folded_days": 0,
        "removed_entries": 0,
        "added_fold_records": 0,
        "files_touched": [],
        "dry_run": dry_run,
        "errors": [],
    }

    if days <= 0:
        result["ok"] = True
        return result

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    audit_files = all_audit_files(root)

    if not audit_files:
        result["ok"] = True
        return result

    changed = False
    for audit_path in audit_files:
        relative_path = audit_path.relative_to(root).as_posix()
        try:
            with private_file_lock(jsonl_lock_path(audit_path), root=root):
                summaries: dict[str, dict[str, Any]] = {}
                for _raw, entry in _iter_audit_rows(audit_path, root=root):
                    if entry is None:
                        continue
                    date_key = _cold_date(entry, cutoff=cutoff)
                    if date_key is None:
                        continue
                    summary = summaries.setdefault(date_key, {"counts": {}, "total": 0})
                    action = entry.get("action")
                    action_key = action if isinstance(action, str) and action else "_unknown"
                    counts = summary["counts"]
                    counts[action_key] = int(counts.get(action_key, 0)) + 1
                    summary["total"] = int(summary["total"]) + 1

                if not summaries:
                    continue

                removed_for_file = sum(int(item["total"]) for item in summaries.values())
                folded_for_file = len(summaries)
                if dry_run:
                    result["folded_days"] += folded_for_file
                    result["removed_entries"] += removed_for_file
                    result["added_fold_records"] += folded_for_file
                    result["files_touched"].append(f"{relative_path} (dry_run)")
                    continue

                def output_lines() -> Iterator[str]:
                    previous_line: str | None = None
                    for raw, entry in _iter_audit_rows(audit_path, root=root):
                        if entry is not None and _cold_date(entry, cutoff=cutoff) is not None:
                            continue
                        if entry is None:
                            rendered = raw
                        else:
                            rendered = _chained_line(entry, previous_line)
                        yield rendered + "\n"
                        previous_line = rendered
                    for date_key in sorted(summaries):
                        rendered = _chained_line(
                            _fold_record(
                                date_key=date_key,
                                summary=summaries[date_key],
                                source_path=relative_path,
                            ),
                            previous_line,
                        )
                        yield rendered + "\n"
                        previous_line = rendered

                atomic_write_private_lines(
                    audit_path,
                    output_lines(),
                    root=root,
                    max_bytes=AUDIT_MAX_BYTES,
                )
                result["folded_days"] += folded_for_file
                result["removed_entries"] += removed_for_file
                result["added_fold_records"] += folded_for_file
                result["files_touched"].append(relative_path)
                changed = True
        except OSError as exc:
            result["errors"].append(f"{relative_path}: {exc}")

    if changed:
        index_result = rebuild_audit_index(root)
        result["audit_index"] = index_result
        if not index_result.get("ok"):
            result["errors"].append(
                f"audit-index: {index_result.get('error', 'rebuild failed')}"
            )

    result["ok"] = not bool(result["errors"])
    return result
