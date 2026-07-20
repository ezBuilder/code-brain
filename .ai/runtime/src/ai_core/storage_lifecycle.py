from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from .private_write import (
    list_root_confined_directory,
    unlink_root_confined_regular_file,
    validate_root_confined_regular_file,
)

LOG_RETENTION_DAYS = 30
LOG_MAX_FILES = 31
LOG_MAX_TOTAL_BYTES = 64 * 1024 * 1024
DIAGNOSTIC_RETENTION_DAYS = 30
DIAGNOSTIC_MAX_FILES = 20
DIAGNOSTIC_MAX_TOTAL_BYTES = 100 * 1024 * 1024
UPGRADE_BACKUP_RETENTION_DAYS = 30
UPGRADE_BACKUP_MAX_FILES = 10
UPGRADE_BACKUP_MAX_TOTAL_BYTES = 20 * 1024 * 1024
_DIRECTORY_SCAN_MAX_ENTRIES = 4096


def _prune_files(
    root: Path,
    directory: Path,
    *,
    accept: Callable[[str], bool],
    keep_days: int,
    max_files: int,
    max_total_bytes: int,
) -> dict[str, int | bool]:
    root = Path(root)
    directory = Path(directory)
    try:
        names = list_root_confined_directory(
            directory,
            root=root,
            max_entries=_DIRECTORY_SCAN_MAX_ENTRIES,
        )
    except FileNotFoundError:
        return {"ok": True, "removed": 0, "kept": 0, "bytes_kept": 0, "errors": 0}
    except OSError:
        return {"ok": False, "removed": 0, "kept": 0, "bytes_kept": 0, "errors": 1}

    candidates: list[tuple[float, int, Path]] = []
    errors = 0
    for name in names:
        if not accept(name):
            continue
        path = directory / name
        try:
            state = validate_root_confined_regular_file(
                path,
                root=root,
                require_owner=True,
                reject_group_other_writable=True,
            )
        except (FileNotFoundError, OSError):
            errors += 1
            continue
        candidates.append((float(state.st_mtime), int(state.st_size), path))

    now = time.time()
    days = max(0, int(keep_days))
    cutoff = now - days * 86400
    file_cap = max(0, int(max_files))
    byte_cap = max(0, int(max_total_bytes))
    removed = 0

    survivors: list[tuple[float, int, Path]] = []
    for item in sorted(candidates, key=lambda value: (value[0], value[2].name), reverse=True):
        mtime, size, path = item
        if mtime < cutoff:
            try:
                if unlink_root_confined_regular_file(path, root=root):
                    removed += 1
            except OSError:
                errors += 1
            continue
        survivors.append(item)

    kept: list[tuple[float, int, Path]] = []
    total = 0
    for item in survivors:
        _mtime, size, path = item
        if len(kept) >= file_cap or total + size > byte_cap:
            try:
                if unlink_root_confined_regular_file(path, root=root):
                    removed += 1
            except OSError:
                errors += 1
            continue
        kept.append(item)
        total += size

    return {
        "ok": errors == 0,
        "removed": removed,
        "kept": len(kept),
        "bytes_kept": total,
        "errors": errors,
    }


def prune_logs(root: Path) -> dict[str, int | bool]:
    return _prune_files(
        root,
        Path(root) / ".ai" / "cache" / "logs",
        accept=lambda name: len(name) == 16 and name.endswith(".jsonl"),
        keep_days=LOG_RETENTION_DAYS,
        max_files=LOG_MAX_FILES,
        max_total_bytes=LOG_MAX_TOTAL_BYTES,
    )


def prune_diagnostics_files(
    root: Path,
    *,
    keep_days: int = DIAGNOSTIC_RETENTION_DAYS,
) -> dict[str, int | bool]:
    return _prune_files(
        root,
        Path(root) / ".ai" / "cache" / "diagnostics",
        accept=lambda name: name.startswith("diagnostics-") and name.endswith((".json", ".zip")),
        keep_days=keep_days,
        max_files=DIAGNOSTIC_MAX_FILES,
        max_total_bytes=DIAGNOSTIC_MAX_TOTAL_BYTES,
    )


def prune_upgrade_backups(root: Path) -> dict[str, int | bool]:
    return _prune_files(
        root,
        Path(root) / ".ai" / "cache" / "upgrade",
        accept=lambda name: name.startswith("rollback-") and name.endswith(".json"),
        keep_days=UPGRADE_BACKUP_RETENTION_DAYS,
        max_files=UPGRADE_BACKUP_MAX_FILES,
        max_total_bytes=UPGRADE_BACKUP_MAX_TOTAL_BYTES,
    )
