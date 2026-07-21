from __future__ import annotations

import os
import shutil
import stat
import subprocess
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
TMP_RETENTION_DAYS = 7
TMP_MAX_ENTRIES = 256
TMP_MAX_TOTAL_BYTES = 512 * 1024 * 1024
OUTPUT_MAX_ENTRIES = 512
OUTPUT_MAX_TOTAL_BYTES = 1024 * 1024 * 1024
AI_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
STORAGE_SCAN_MAX_ENTRIES = 200_000
_DIRECTORY_SCAN_MAX_ENTRIES = 4096


def _tree_usage(path: Path, *, max_entries: int = STORAGE_SCAN_MAX_ENTRIES) -> dict[str, int | bool]:
    if not path.exists() and not path.is_symlink():
        return {"bytes": 0, "entries": 0, "newest_mtime_ns": 0, "complete": True, "errors": 0}

    stack = [path]
    total = 0
    entries = 0
    newest_mtime_ns = 0
    errors = 0
    complete = True
    while stack:
        current = stack.pop()
        try:
            state = current.lstat()
        except OSError:
            errors += 1
            continue
        entries += 1
        if entries > max_entries:
            complete = False
            break
        newest_mtime_ns = max(newest_mtime_ns, int(state.st_mtime_ns))
        if stat.S_ISREG(state.st_mode):
            total += int(state.st_size)
            continue
        if stat.S_ISLNK(state.st_mode):
            total += int(state.st_size)
            continue
        if not stat.S_ISDIR(state.st_mode):
            continue
        try:
            with os.scandir(current) as children:
                stack.extend(Path(child.path) for child in children)
        except OSError:
            errors += 1

    return {
        "bytes": total,
        "entries": entries,
        "newest_mtime_ns": newest_mtime_ns,
        "complete": complete,
        "errors": errors,
    }


def workspace_storage_status(root: Path) -> dict[str, int | bool]:
    root = Path(root)
    ai = _tree_usage(root / ".ai")
    tmp = _tree_usage(root / ".ai" / "tmp")
    outputs = _tree_usage(root / ".ai" / "outputs")
    try:
        tmp_top_entries = len(list_root_confined_directory(root / ".ai" / "tmp", root=root, max_entries=_DIRECTORY_SCAN_MAX_ENTRIES))
    except FileNotFoundError:
        tmp_top_entries = 0
    except OSError:
        tmp_top_entries = TMP_MAX_ENTRIES + 1
    try:
        output_top_entries = len(list_root_confined_directory(root / ".ai" / "outputs", root=root, max_entries=_DIRECTORY_SCAN_MAX_ENTRIES))
    except FileNotFoundError:
        output_top_entries = 0
    except OSError:
        output_top_entries = OUTPUT_MAX_ENTRIES + 1
    complete = bool(ai["complete"] and tmp["complete"] and outputs["complete"])
    errors = int(ai["errors"]) + int(tmp["errors"]) + int(outputs["errors"])
    ai_bytes = int(ai["bytes"])
    tmp_bytes = int(tmp["bytes"])
    output_bytes = int(outputs["bytes"])
    return {
        "ok": complete
        and errors == 0
        and ai_bytes <= AI_MAX_TOTAL_BYTES
        and tmp_bytes <= TMP_MAX_TOTAL_BYTES
        and tmp_top_entries <= TMP_MAX_ENTRIES
        and output_bytes <= OUTPUT_MAX_TOTAL_BYTES
        and output_top_entries <= OUTPUT_MAX_ENTRIES,
        "complete": complete,
        "errors": errors,
        "ai_bytes": ai_bytes,
        "ai_max_bytes": AI_MAX_TOTAL_BYTES,
        "tmp_bytes": tmp_bytes,
        "tmp_max_bytes": TMP_MAX_TOTAL_BYTES,
        "tmp_top_entries": tmp_top_entries,
        "tmp_max_entries": TMP_MAX_ENTRIES,
        "output_bytes": output_bytes,
        "output_max_bytes": OUTPUT_MAX_TOTAL_BYTES,
        "output_top_entries": output_top_entries,
        "output_max_entries": OUTPUT_MAX_ENTRIES,
        "entries_scanned": int(ai["entries"]),
    }


def _tracked_top_entries(root: Path, directory: Path) -> tuple[set[str], bool]:
    try:
        rel = directory.relative_to(root).as_posix()
    except ValueError:
        return set(), False
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--", rel],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return set(), False
    if result.returncode != 0 or len(result.stdout) > 4 * 1024 * 1024:
        return set(), False
    prefix = tuple(Path(rel).parts)
    names: set[str] = set()
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        try:
            parts = Path(raw.decode("utf-8")).parts
        except UnicodeDecodeError:
            return set(), False
        if parts[: len(prefix)] == prefix and len(parts) > len(prefix):
            names.add(parts[len(prefix)])
    return names, True


def _has_keep_marker(path: Path) -> bool:
    if path.name.endswith(".keep") or path.name == ".gitkeep":
        return True
    markers = [path.with_name(path.name + ".keep")]
    try:
        if stat.S_ISDIR(path.lstat().st_mode):
            markers.append(path / ".keep")
    except OSError:
        return True
    for marker in markers:
        try:
            if stat.S_ISREG(marker.lstat().st_mode):
                return True
        except OSError:
            continue
    return path.name == ".keep"


def _managed_entries(root: Path, directory: Path) -> tuple[list[dict[str, object]], int]:
    try:
        names = list_root_confined_directory(directory, root=root, max_entries=_DIRECTORY_SCAN_MAX_ENTRIES)
    except FileNotFoundError:
        return [], 0
    except OSError:
        return [], 1
    tracked, tracked_known = _tracked_top_entries(root, directory)
    rows: list[dict[str, object]] = []
    errors = 0
    for name in names:
        path = directory / name
        usage = _tree_usage(path)
        if not usage["complete"] or usage["errors"]:
            errors += int(usage["errors"]) + (0 if usage["complete"] else 1)
        rows.append(
            {
                "path": path,
                "name": name,
                "bytes": int(usage["bytes"]),
                "mtime_ns": int(usage["newest_mtime_ns"]),
                "pinned": not tracked_known or name in tracked or _has_keep_marker(path),
            }
        )
    return rows, errors


def _remove_managed_entry(path: Path, *, root: Path) -> bool:
    try:
        root_real = root.resolve(strict=True)
        path.parent.resolve(strict=True).relative_to(root_real)
        state = path.lstat()
        if stat.S_ISDIR(state.st_mode) and not stat.S_ISLNK(state.st_mode):
            shutil.rmtree(path)
        else:
            path.unlink()
        return True
    except (FileNotFoundError, OSError, ValueError):
        return False


def _prune_managed_directory(
    root: Path,
    directory: Path,
    *,
    keep_days: int | None,
    max_entries: int,
    max_total_bytes: int,
) -> dict[str, int | bool]:
    rows, errors = _managed_entries(root, directory)
    bytes_before = sum(int(row["bytes"]) for row in rows)
    removed = 0
    removed_bytes = 0
    cutoff_ns = time.time_ns() - max(0, int(keep_days or 0)) * 86_400 * 1_000_000_000
    survivors: list[dict[str, object]] = []
    for row in sorted(rows, key=lambda value: (int(value["mtime_ns"]), str(value["name"]))):
        expired = keep_days is not None and int(row["mtime_ns"]) < cutoff_ns
        if expired and not bool(row["pinned"]):
            if _remove_managed_entry(Path(row["path"]), root=root):
                removed += 1
                removed_bytes += int(row["bytes"])
                continue
            errors += 1
        survivors.append(row)

    total = sum(int(row["bytes"]) for row in survivors)
    count = len(survivors)
    kept: list[dict[str, object]] = []
    for row in survivors:
        over = count > max(0, int(max_entries)) or total > max(0, int(max_total_bytes))
        if over and not bool(row["pinned"]):
            if _remove_managed_entry(Path(row["path"]), root=root):
                removed += 1
                removed_bytes += int(row["bytes"])
                total -= int(row["bytes"])
                count -= 1
                continue
            errors += 1
        kept.append(row)

    return {
        "ok": errors == 0 and total <= max_total_bytes and count <= max_entries,
        "removed": removed,
        "removed_bytes": removed_bytes,
        "bytes_before": bytes_before,
        "bytes_kept": total,
        "kept": count,
        "pinned": sum(1 for row in kept if row["pinned"]),
        "errors": errors,
    }


def _reclaim_from_directory(root: Path, directory: Path, *, needed_bytes: int) -> dict[str, int | bool]:
    rows, errors = _managed_entries(root, directory)
    reclaimed = 0
    removed = 0
    for row in sorted(rows, key=lambda value: (int(value["mtime_ns"]), str(value["name"]))):
        if reclaimed >= needed_bytes:
            break
        if bool(row["pinned"]):
            continue
        if _remove_managed_entry(Path(row["path"]), root=root):
            reclaimed += int(row["bytes"])
            removed += 1
        else:
            errors += 1
    return {"ok": errors == 0, "removed": removed, "reclaimed_bytes": reclaimed, "errors": errors}


def enforce_workspace_storage(root: Path) -> dict[str, object]:
    root = Path(root)
    tmp = _prune_managed_directory(
        root,
        root / ".ai" / "tmp",
        keep_days=TMP_RETENTION_DAYS,
        max_entries=TMP_MAX_ENTRIES,
        max_total_bytes=TMP_MAX_TOTAL_BYTES,
    )
    outputs = _prune_managed_directory(
        root,
        root / ".ai" / "outputs",
        keep_days=None,
        max_entries=OUTPUT_MAX_ENTRIES,
        max_total_bytes=OUTPUT_MAX_TOTAL_BYTES,
    )
    status = workspace_storage_status(root)
    reclaim: list[dict[str, object]] = []
    excess = max(0, int(status["ai_bytes"]) - AI_MAX_TOTAL_BYTES)
    for directory in (root / ".ai" / "tmp", root / ".ai" / "outputs"):
        if excess <= 0:
            break
        result = _reclaim_from_directory(root, directory, needed_bytes=excess)
        reclaim.append({"directory": directory.relative_to(root).as_posix(), **result})
        excess = max(0, excess - int(result["reclaimed_bytes"]))
    if reclaim:
        status = workspace_storage_status(root)
    return {
        "ok": bool(tmp["ok"] and outputs["ok"] and status["ok"]),
        "tmp": tmp,
        "outputs": outputs,
        "reclaim": reclaim,
        "status": status,
    }


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
