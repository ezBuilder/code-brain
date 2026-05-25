"""
Fold cold audit entries (30+ days old) into daily summary records.

Reduces storage and memory peak by compressing historical audit log entries
into one fold record per day, preserving action counts and metadata.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .memory import all_audit_files


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

    for audit_path in audit_files:
        if not audit_path.exists():
            continue

        try:
            entries: list[dict[str, Any]] = []
            for line in audit_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if not isinstance(entry, dict):
                        entries.append(entry)
                        continue
                    entries.append(entry)
                except json.JSONDecodeError:
                    entries.append({"_malformed": True, "_raw": line})

            # Partition into cold and recent
            cold_entries: dict[str, list[dict[str, Any]]] = {}
            recent_entries: list[dict[str, Any]] = []

            for entry in entries:
                if entry.get("_malformed"):
                    recent_entries.append(entry)
                    continue

                ts_str = entry.get("ts")
                ts = _parse_ts(ts_str)

                # Skip entries already folded (idempotent)
                if entry.get("action") == "_folded":
                    recent_entries.append(entry)
                    continue

                if ts and ts < cutoff:
                    date_key = _date_from_ts(ts)
                    if date_key not in cold_entries:
                        cold_entries[date_key] = []
                    cold_entries[date_key].append(entry)
                else:
                    recent_entries.append(entry)

            if not cold_entries:
                continue

            # Build fold records
            fold_records: list[dict[str, Any]] = []
            for date_key in sorted(cold_entries.keys()):
                old_entries = cold_entries[date_key]
                action_counts: dict[str, int] = {}
                for entry in old_entries:
                    action = entry.get("action", "_unknown")
                    action_counts[action] = action_counts.get(action, 0) + 1

                fold_record = {
                    "action": "_folded",
                    "payload": {
                        "date": date_key,
                        "counts": action_counts,
                        "total": len(old_entries),
                        "source_files": [audit_path.relative_to(root).as_posix()],
                    },
                    "ts": f"{date_key}T23:59:59Z",
                }
                fold_records.append(fold_record)
                result["folded_days"] += 1
                result["removed_entries"] += len(old_entries)
                result["added_fold_records"] += 1

            if not dry_run:
                # Write atomically: temp file, then replace
                try:
                    temp_fd, temp_path = tempfile.mkstemp(
                        suffix=".jsonl",
                        dir=audit_path.parent,
                        text=True,
                    )
                    with os.fdopen(temp_fd, "w", encoding="utf-8") as tmp:
                        for entry in recent_entries:
                            if entry.get("_malformed"):
                                # Preserve malformed lines as-is
                                tmp.write(entry.get("_raw", "") + "\n")
                            else:
                                line = json.dumps(
                                    entry,
                                    ensure_ascii=False,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                )
                                tmp.write(line + "\n")
                        for fold in fold_records:
                            line = json.dumps(
                                fold,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                            tmp.write(line + "\n")

                    os.replace(temp_path, audit_path)
                    result["files_touched"].append(audit_path.relative_to(root).as_posix())
                except OSError as e:
                    result["errors"].append(f"{audit_path.relative_to(root).as_posix()}: {e}")
            else:
                # dry_run: just count, no writes
                result["files_touched"].append(f"{audit_path.relative_to(root).as_posix()} (dry_run)")

        except OSError as e:
            result["errors"].append(f"{audit_path.relative_to(root).as_posix()}: {e}")
            continue

    result["ok"] = not bool(result["errors"])
    return result
