"""
Fold cold audit entries (30+ days old) into daily summary records.

Reduces storage and memory peak by compressing historical audit log entries
into one fold record per day, preserving action counts and metadata.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .memory import (
    _rebuild_audit_index_locked,
    all_audit_files,
    audit_transaction_lock_path,
    jsonl_lock_path,
    read_state_text,
)
from .private_write import atomic_write_private_text, private_file_lock


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


def _fold_one_file(
    root: Path,
    audit_path: Path,
    *,
    cutoff: datetime,
    dry_run: bool,
) -> dict[str, Any]:
    """Fold one audit file while holding the same lock used by appenders."""
    rel = audit_path.relative_to(root).as_posix()
    with private_file_lock(jsonl_lock_path(audit_path), root=root):
        audit_text = read_state_text(audit_path, max_bytes=100_000_000)
        recent_entries: list[dict[str, Any]] = []
        cold_entries: dict[str, list[dict[str, Any]]] = {}

        for raw_line in audit_text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                loaded = json.loads(line)
            except json.JSONDecodeError:
                recent_entries.append({"_malformed": True, "_raw": line})
                continue
            if not isinstance(loaded, dict):
                recent_entries.append({"_malformed": True, "_raw": line})
                continue
            if loaded.get("action") == "_folded":
                recent_entries.append(loaded)
                continue
            ts = _parse_ts(loaded.get("ts"))
            if ts is None or ts >= cutoff:
                recent_entries.append(loaded)
                continue
            cold_entries.setdefault(_date_from_ts(ts), []).append(loaded)

        if not cold_entries:
            return {
                "folded_days": 0,
                "removed_entries": 0,
                "added_fold_records": 0,
                "touched": None,
            }

        fold_records: list[dict[str, Any]] = []
        removed_entries = 0
        for date_key in sorted(cold_entries):
            old_entries = cold_entries[date_key]
            action_counts: dict[str, int] = {}
            for entry in old_entries:
                action = str(entry.get("action") or "_unknown")
                action_counts[action] = action_counts.get(action, 0) + 1
            fold_records.append(
                {
                    "action": "_folded",
                    "payload": {
                        "date": date_key,
                        "counts": action_counts,
                        "total": len(old_entries),
                        "source_files": [rel],
                    },
                    "ts": f"{date_key}T23:59:59Z",
                }
            )
            removed_entries += len(old_entries)

        if not dry_run:
            output_lines: list[str] = []
            for entry in recent_entries:
                if entry.get("_malformed"):
                    output_lines.append(str(entry.get("_raw", "")))
                else:
                    output_lines.append(
                        json.dumps(
                            entry,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    )
            output_lines.extend(
                json.dumps(
                    fold,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                for fold in fold_records
            )
            output = "\n".join(output_lines) + ("\n" if output_lines else "")
            atomic_write_private_text(audit_path, output, root=root)

        return {
            "folded_days": len(fold_records),
            "removed_entries": removed_entries,
            "added_fold_records": len(fold_records),
            "touched": f"{rel} (dry_run)" if dry_run else rel,
        }


def _fold_files_locked(
    root: Path,
    *,
    cutoff: datetime,
    dry_run: bool,
    result: dict[str, Any],
) -> None:
    """Fold all current files while the global audit transaction lock is held."""
    for audit_path in all_audit_files(root):
        try:
            folded = _fold_one_file(
                root,
                audit_path,
                cutoff=cutoff,
                dry_run=dry_run,
            )
        except (OSError, UnicodeDecodeError) as exc:
            result["errors"].append(
                f"{audit_path.relative_to(root).as_posix()}: {type(exc).__name__}"
            )
            continue
        result["folded_days"] += int(folded["folded_days"])
        result["removed_entries"] += int(folded["removed_entries"])
        result["added_fold_records"] += int(folded["added_fold_records"])
        if folded["touched"]:
            result["files_touched"].append(str(folded["touched"]))

    if not dry_run and result["files_touched"] and not result["errors"]:
        index_result = _rebuild_audit_index_locked(root)
        if not index_result.get("ok"):
            result["errors"].append("audit index rebuild failed")


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

    root = Path(root)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        with private_file_lock(audit_transaction_lock_path(root), root=root):
            _fold_files_locked(
                root,
                cutoff=cutoff,
                dry_run=dry_run,
                result=result,
            )
    except OSError as exc:
        result["errors"].append(f"audit transaction: {type(exc).__name__}")

    result["ok"] = not bool(result["errors"])
    return result
