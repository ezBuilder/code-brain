"""
Fold cold audit entries (30+ days old) into daily summary records.

Reduces storage and memory peak by compressing historical audit log entries
into one fold record per day, preserving action counts and metadata.
"""

from __future__ import annotations

import json
import os
import stat
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .loss_accounting import finalize_event, loss_event
from .memory import AUDIT_MAX_BYTES
from .private_write import atomic_write_private_text, read_root_confined_text


AUDIT_FOLD_MAX_FILES = 256
AUDIT_FOLD_MAX_CANDIDATES = 1024
AUDIT_FOLD_MAX_BYTES = 256_000_000
AUDIT_FOLD_MAX_SECONDS = 5.0


def _bounded_audit_inventory(root: Path) -> tuple[list[tuple[Path, os.stat_result]], list[str], dict[str, Any]]:
    directory = Path(root) / ".ai" / "memory" / "audit"
    policy = {
        "max_files": int(AUDIT_FOLD_MAX_FILES),
        "max_candidates": int(AUDIT_FOLD_MAX_CANDIDATES),
        "max_bytes": int(AUDIT_FOLD_MAX_BYTES),
        "max_seconds": float(AUDIT_FOLD_MAX_SECONDS),
    }
    if not directory.exists():
        return [], [], {
            "bounded": True,
            "complete": True,
            "candidates_scanned": 0,
            "files": 0,
            "bytes": 0,
            "elapsed_ms": 0,
            "policy": policy,
        }
    started = time.monotonic()
    deadline = started + max(0.05, float(AUDIT_FOLD_MAX_SECONDS))
    candidates = 0
    total_bytes = 0
    files: list[tuple[Path, os.stat_result]] = []
    errors: list[str] = []
    complete = True
    try:
        entries = os.scandir(directory)
    except OSError as exc:
        return [], [f"list:{exc}"], {
            "bounded": True,
            "complete": False,
            "candidates_scanned": 0,
            "files": 0,
            "bytes": 0,
            "elapsed_ms": 0,
            "policy": policy,
        }
    try:
        with entries:
            for entry in entries:
                if candidates >= max(1, int(AUDIT_FOLD_MAX_CANDIDATES)):
                    errors.append("scan:candidate_limit")
                    complete = False
                    break
                if time.monotonic() >= deadline:
                    errors.append("scan:time_limit")
                    complete = False
                    break
                candidates += 1
                if not entry.name.endswith(".jsonl"):
                    continue
                path = Path(entry.path)
                try:
                    state = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    errors.append(f"{entry.name}:stat:{exc}")
                    complete = False
                    continue
                if stat.S_ISLNK(state.st_mode):
                    errors.append(f"{entry.name}:unsafe-symlink")
                    complete = False
                    continue
                if not stat.S_ISREG(state.st_mode):
                    errors.append(f"{entry.name}:not-regular")
                    complete = False
                    continue
                if int(getattr(state, "st_nlink", 1)) != 1:
                    errors.append(f"{entry.name}:unsafe-hardlink")
                    complete = False
                    continue
                next_files = len(files) + 1
                next_bytes = total_bytes + int(state.st_size)
                if next_files > max(1, int(AUDIT_FOLD_MAX_FILES)):
                    errors.append("scan:file_limit")
                    complete = False
                    break
                if next_bytes > max(1, int(AUDIT_FOLD_MAX_BYTES)):
                    errors.append("scan:byte_limit")
                    complete = False
                    break
                if int(state.st_size) > max(1024, int(AUDIT_MAX_BYTES)):
                    errors.append(f"{entry.name}:oversized")
                    complete = False
                    continue
                files.append((path, state))
                total_bytes = next_bytes
    except OSError as exc:
        errors.append(f"scan:{exc}")
        complete = False
    files.sort(key=lambda item: item[0].name)
    return files, errors, {
        "bounded": True,
        "complete": complete,
        "candidates_scanned": candidates,
        "files": len(files),
        "bytes": total_bytes,
        "elapsed_ms": max(0, int((time.monotonic() - started) * 1000)),
        "policy": policy,
    }


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
        "scan": {},
    }

    before_files = 0
    before_bytes = 0
    before_records = 0
    after_bytes = 0
    after_records = 0

    def finish() -> dict[str, Any]:
        loss = finalize_event(
            root,
            loss_event(
                domain="audit_fold",
                operation=".ai/memory/audit",
                applied=not dry_run and result["removed_entries"] > 0 and not result["errors"],
                dry_run=dry_run,
                files_before=before_files,
                files_after=before_files,
                bytes_before=before_bytes,
                bytes_after=after_bytes,
                records_before=before_records,
                records_after=after_records,
                reasons={"age_fold": result["removed_entries"]},
                errors=result["errors"],
                examples=result["files_touched"],
            ),
        )
        result["loss"] = loss
        result["ok"] = (
            not bool(result["errors"])
            and loss.get("accounting", {}).get("ok") is True
        )
        return result

    if days <= 0:
        return finish()

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    inventory, inventory_errors, scan = _bounded_audit_inventory(root)
    result["scan"] = scan
    before_files = len(inventory)
    before_bytes = sum(int(state.st_size) for _path, state in inventory)
    after_bytes = before_bytes
    if scan.get("complete") is not True or inventory_errors:
        result["errors"].extend(inventory_errors or ["scan_incomplete"])
        return finish()

    if not inventory:
        return finish()

    after_bytes = 0
    for audit_path, expected_state in inventory:
        try:
            raw_text, stable_state = read_root_confined_text(
                audit_path,
                root=root,
                max_bytes=max(1024, int(AUDIT_MAX_BYTES)),
                # The inventory already enforces no symlink, regular file,
                # single hard link and inode stability. Accept legacy 0644
                # audit files so the fold can migrate them through the private
                # atomic writer instead of becoming permanently unusable.
                require_private=False,
            )
            if (
                int(stable_state.st_dev) != int(expected_state.st_dev)
                or int(stable_state.st_ino) != int(expected_state.st_ino)
            ):
                raise OSError("audit file changed after inventory")
            file_before_bytes = len(raw_text.encode("utf-8"))
            entries: list[dict[str, Any]] = []
            for line in raw_text.splitlines():
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
            before_records += len(entries)

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
                after_bytes += file_before_bytes
                after_records += len(entries)
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
            file_removed = sum(len(items) for items in cold_entries.values())
            file_added = len(fold_records)
            rendered_lines: list[str] = []
            for entry in recent_entries:
                if entry.get("_malformed"):
                    rendered_lines.append(str(entry.get("_raw", "")))
                else:
                    rendered_lines.append(
                        json.dumps(
                            entry,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    )
            rendered_lines.extend(
                json.dumps(
                    fold,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                for fold in fold_records
            )
            replacement = ("\n".join(rendered_lines) + "\n") if rendered_lines else ""
            replacement_bytes = len(replacement.encode("utf-8"))

            if not dry_run:
                try:
                    current = audit_path.lstat()
                    if (
                        stat.S_ISLNK(current.st_mode)
                        or not stat.S_ISREG(current.st_mode)
                        or int(getattr(current, "st_nlink", 1)) != 1
                        or int(current.st_dev) != int(expected_state.st_dev)
                        or int(current.st_ino) != int(expected_state.st_ino)
                    ):
                        raise OSError("audit file changed before rewrite")
                    atomic_write_private_text(audit_path, replacement, root=root)
                    result["files_touched"].append(audit_path.relative_to(root).as_posix())
                    result["folded_days"] += len(fold_records)
                    result["removed_entries"] += file_removed
                    result["added_fold_records"] += file_added
                    after_bytes += replacement_bytes
                    after_records += len(rendered_lines)
                except OSError as e:
                    result["errors"].append(f"{audit_path.relative_to(root).as_posix()}: {e}")
                    after_bytes += file_before_bytes
                    after_records += len(entries)
            else:
                # dry_run: just count, no writes
                result["files_touched"].append(f"{audit_path.relative_to(root).as_posix()} (dry_run)")
                result["folded_days"] += len(fold_records)
                result["removed_entries"] += file_removed
                result["added_fold_records"] += file_added
                after_bytes += replacement_bytes
                after_records += len(rendered_lines)

        except OSError as e:
            result["errors"].append(f"{audit_path.relative_to(root).as_posix()}: {e}")
            after_bytes += int(expected_state.st_size)
            continue

    return finish()
