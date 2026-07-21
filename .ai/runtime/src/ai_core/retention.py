from __future__ import annotations

import os
import stat
import time
from pathlib import Path
from typing import Any, Iterable

from .private_write import validate_root_confined_directory


def _matches(path: Path, *, prefixes: tuple[str, ...], suffixes: tuple[str, ...]) -> bool:
    if prefixes and not any(path.name.startswith(prefix) for prefix in prefixes):
        return False
    if suffixes and path.suffix not in suffixes:
        return False
    return True


def _scan_files(
    root: Path,
    directory: Path,
    *,
    prefixes: tuple[str, ...],
    suffixes: tuple[str, ...],
) -> tuple[list[tuple[Path, os.stat_result]], list[str]]:
    root = Path(root)
    directory = Path(directory)
    try:
        validate_root_confined_directory(
            directory,
            root=root,
            require_safe_permissions=False,
        )
    except FileNotFoundError:
        return [], []
    except OSError as exc:
        return [], [f"directory:{exc}"]

    files: list[tuple[Path, os.stat_result]] = []
    errors: list[str] = []
    try:
        children = list(directory.iterdir())
    except OSError as exc:
        return [], [f"list:{exc}"]
    for path in children:
        if not _matches(path, prefixes=prefixes, suffixes=suffixes):
            continue
        try:
            state = path.lstat()
        except OSError as exc:
            errors.append(f"{path.name}:stat:{exc}")
            continue
        if stat.S_ISLNK(state.st_mode):
            errors.append(f"{path.name}:unsafe-symlink")
            continue
        if not stat.S_ISREG(state.st_mode):
            errors.append(f"{path.name}:not-regular")
            continue
        if int(getattr(state, "st_nlink", 1)) != 1:
            errors.append(f"{path.name}:unsafe-hardlink")
            continue
        files.append((path, state))
    files.sort(key=lambda item: (int(item[1].st_mtime_ns), item[0].name), reverse=True)
    return files, errors


def retention_status(
    root: Path,
    directory: Path,
    *,
    prefixes: Iterable[str] = (),
    suffixes: Iterable[str] = (),
    keep_days: int,
    max_files: int,
    max_bytes: int,
    now: float | None = None,
) -> dict[str, Any]:
    files, errors = _scan_files(
        root,
        directory,
        prefixes=tuple(prefixes),
        suffixes=tuple(suffixes),
    )
    effective_now = time.time() if now is None else float(now)
    cutoff = effective_now - max(0, int(keep_days)) * 86400
    count = len(files)
    total_bytes = sum(int(state.st_size) for _path, state in files)
    expired = [path.name for path, state in files if float(state.st_mtime) < cutoff]
    violations: list[str] = []
    if expired:
        violations.append(f"expired={len(expired)}")
    if count > max(0, int(max_files)):
        violations.append(f"files={count}>{max(0, int(max_files))}")
    if total_bytes > max(0, int(max_bytes)):
        violations.append(f"bytes={total_bytes}>{max(0, int(max_bytes))}")
    if errors:
        violations.append(f"unsafe={len(errors)}")
    return {
        "ok": not violations,
        "directory": Path(directory).relative_to(root).as_posix(),
        "count": count,
        "bytes": total_bytes,
        "keep_days": max(0, int(keep_days)),
        "max_files": max(0, int(max_files)),
        "max_bytes": max(0, int(max_bytes)),
        "expired": expired,
        "violations": violations,
        "errors": errors,
    }


def prune_directory(
    root: Path,
    directory: Path,
    *,
    prefixes: Iterable[str] = (),
    suffixes: Iterable[str] = (),
    keep_days: int,
    max_files: int,
    max_bytes: int,
    preserve: Iterable[Path] = (),
    dry_run: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
    prefix_tuple = tuple(prefixes)
    suffix_tuple = tuple(suffixes)
    files, scan_errors = _scan_files(
        root,
        directory,
        prefixes=prefix_tuple,
        suffixes=suffix_tuple,
    )
    effective_now = time.time() if now is None else float(now)
    cutoff = effective_now - max(0, int(keep_days)) * 86400
    preserved = {Path(path).absolute() for path in preserve}
    selected: set[Path] = set()

    for path, state in files:
        if path.absolute() not in preserved and float(state.st_mtime) < cutoff:
            selected.add(path)

    remaining = [(path, state) for path, state in files if path not in selected]
    file_limit = max(0, int(max_files))
    while len(remaining) > file_limit:
        candidate = next(
            (item for item in reversed(remaining) if item[0].absolute() not in preserved),
            None,
        )
        if candidate is None:
            break
        selected.add(candidate[0])
        remaining.remove(candidate)

    byte_limit = max(0, int(max_bytes))
    remaining_bytes = sum(int(state.st_size) for _path, state in remaining)
    while remaining and remaining_bytes > byte_limit:
        candidate = next(
            (item for item in reversed(remaining) if item[0].absolute() not in preserved),
            None,
        )
        if candidate is None:
            break
        selected.add(candidate[0])
        remaining.remove(candidate)
        remaining_bytes -= int(candidate[1].st_size)

    removed: list[str] = []
    removed_bytes = 0
    errors = list(scan_errors)
    expected = {path: state for path, state in files}
    for path in sorted(selected, key=lambda item: item.name):
        state = expected[path]
        removed_bytes += int(state.st_size)
        if dry_run:
            removed.append(path.name)
            continue
        try:
            current = path.lstat()
            if (
                stat.S_ISLNK(current.st_mode)
                or not stat.S_ISREG(current.st_mode)
                or int(getattr(current, "st_nlink", 1)) != 1
                or int(current.st_dev) != int(state.st_dev)
                or int(current.st_ino) != int(state.st_ino)
            ):
                errors.append(f"{path.name}:changed-before-delete")
                continue
            path.unlink()
            removed.append(path.name)
        except OSError as exc:
            errors.append(f"{path.name}:delete:{exc}")

    after = retention_status(
        root,
        directory,
        prefixes=prefix_tuple,
        suffixes=suffix_tuple,
        keep_days=keep_days,
        max_files=max_files,
        max_bytes=max_bytes,
        now=effective_now,
    )
    if dry_run:
        after = {
            **after,
            "ok": not errors,
            "projected_count": len(remaining),
            "projected_bytes": remaining_bytes,
        }
    return {
        "ok": bool(after.get("ok")) and not errors,
        "dry_run": dry_run,
        "removed": removed,
        "removed_count": len(removed),
        "removed_bytes": removed_bytes,
        "errors": errors,
        "status": after,
    }


__all__ = ["prune_directory", "retention_status"]
