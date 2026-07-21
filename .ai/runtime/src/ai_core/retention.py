from __future__ import annotations

import os
import stat
import time
from pathlib import Path
from typing import Any, Iterable

from .loss_accounting import finalize_event, loss_event
from .private_write import validate_root_confined_directory


def _bounded_env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _bounded_env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


RETENTION_SCAN_MAX_CANDIDATES = _bounded_env_int(
    "AI_RETENTION_SCAN_MAX_CANDIDATES",
    20_000,
    minimum=1,
    maximum=1_000_000,
)
RETENTION_SCAN_MAX_SECONDS = _bounded_env_float(
    "AI_RETENTION_SCAN_MAX_SECONDS",
    2.0,
    minimum=0.05,
    maximum=60.0,
)


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
) -> tuple[list[tuple[Path, os.stat_result]], list[str], dict[str, Any]]:
    root = Path(root)
    directory = Path(directory)
    try:
        validate_root_confined_directory(
            directory,
            root=root,
            require_safe_permissions=False,
        )
    except FileNotFoundError:
        return [], [], {
            "bounded": True,
            "complete": True,
            "candidates_scanned": 0,
            "policy": {
                "max_candidates": int(RETENTION_SCAN_MAX_CANDIDATES),
                "max_seconds": float(RETENTION_SCAN_MAX_SECONDS),
            },
        }
    except OSError as exc:
        return [], [f"directory:{exc}"], {
            "bounded": True,
            "complete": False,
            "candidates_scanned": 0,
            "policy": {
                "max_candidates": int(RETENTION_SCAN_MAX_CANDIDATES),
                "max_seconds": float(RETENTION_SCAN_MAX_SECONDS),
            },
        }

    files: list[tuple[Path, os.stat_result]] = []
    errors: list[str] = []
    started = time.monotonic()
    deadline = started + float(RETENTION_SCAN_MAX_SECONDS)
    candidates_scanned = 0
    complete = True
    try:
        entries = os.scandir(directory)
    except OSError as exc:
        return [], [f"list:{exc}"], {
            "bounded": True,
            "complete": False,
            "candidates_scanned": 0,
            "policy": {
                "max_candidates": int(RETENTION_SCAN_MAX_CANDIDATES),
                "max_seconds": float(RETENTION_SCAN_MAX_SECONDS),
            },
        }
    try:
        with entries:
            for entry in entries:
                if candidates_scanned >= int(RETENTION_SCAN_MAX_CANDIDATES):
                    errors.append("scan:candidate_limit")
                    complete = False
                    break
                if time.monotonic() >= deadline:
                    errors.append("scan:time_limit")
                    complete = False
                    break
                candidates_scanned += 1
                path = Path(entry.path)
                if not _matches(path, prefixes=prefixes, suffixes=suffixes):
                    continue
                try:
                    state = entry.stat(follow_symlinks=False)
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
    except OSError as exc:
        errors.append(f"scan:{exc}")
        complete = False
    files.sort(key=lambda item: (int(item[1].st_mtime_ns), item[0].name), reverse=True)
    return files, errors, {
        "bounded": True,
        "complete": complete,
        "candidates_scanned": candidates_scanned,
        "elapsed_ms": max(0, int((time.monotonic() - started) * 1000)),
        "policy": {
            "max_candidates": int(RETENTION_SCAN_MAX_CANDIDATES),
            "max_seconds": float(RETENTION_SCAN_MAX_SECONDS),
        },
    }


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
    files, errors, scan = _scan_files(
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
        "scan": scan,
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
    accounting_domain: str = "runtime_retention",
) -> dict[str, Any]:
    prefix_tuple = tuple(prefixes)
    suffix_tuple = tuple(suffixes)
    files, scan_errors, scan = _scan_files(
        root,
        directory,
        prefixes=prefix_tuple,
        suffixes=suffix_tuple,
    )
    effective_now = time.time() if now is None else float(now)
    cutoff = effective_now - max(0, int(keep_days)) * 86400
    preserved = {Path(path).absolute() for path in preserve}
    selected: set[Path] = set()
    selected_reasons: dict[Path, set[str]] = {}
    before_count = len(files)
    before_bytes = sum(int(state.st_size) for _path, state in files)

    if scan.get("complete") is not True:
        loss = finalize_event(
            root,
            loss_event(
                domain=accounting_domain,
                operation=Path(directory).relative_to(root).as_posix(),
                applied=False,
                files_before=before_count,
                files_after=before_count,
                bytes_before=before_bytes,
                bytes_after=before_bytes,
                errors=scan_errors or ("scan_incomplete",),
            ),
        )
        return {
            "ok": False,
            "directory": Path(directory).relative_to(root).as_posix(),
            "dry_run": dry_run,
            "removed": [],
            "removed_count": 0,
            "removed_bytes": 0,
            "errors": scan_errors or ["scan_incomplete"],
            "scan": scan,
            "status": {
                "ok": False,
                "count": before_count,
                "bytes": before_bytes,
                "violations": ["scan_incomplete"],
                "errors": scan_errors,
                "scan": scan,
            },
            "loss": loss,
        }

    def select(path: Path, reason: str) -> None:
        selected.add(path)
        selected_reasons.setdefault(path, set()).add(reason)

    for path, state in files:
        if path.absolute() not in preserved and float(state.st_mtime) < cutoff:
            select(path, "age_limit")

    remaining = [(path, state) for path, state in files if path not in selected]
    file_limit = max(0, int(max_files))
    while len(remaining) > file_limit:
        candidate = next(
            (item for item in reversed(remaining) if item[0].absolute() not in preserved),
            None,
        )
        if candidate is None:
            break
        select(candidate[0], "file_limit")
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
        select(candidate[0], "byte_limit")
        remaining.remove(candidate)
        remaining_bytes -= int(candidate[1].st_size)

    removed: list[str] = []
    removed_bytes = 0
    errors = list(scan_errors)
    expected = {path: state for path, state in files}
    for path in sorted(selected, key=lambda item: item.name):
        state = expected[path]
        if dry_run:
            removed.append(path.name)
            removed_bytes += int(state.st_size)
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
            removed_bytes += int(state.st_size)
        except OSError as exc:
            errors.append(f"{path.name}:delete:{exc}")

    if dry_run:
        after = {
            "ok": not errors,
            "directory": Path(directory).relative_to(root).as_posix(),
            "count": before_count,
            "bytes": before_bytes,
            "keep_days": max(0, int(keep_days)),
            "max_files": file_limit,
            "max_bytes": byte_limit,
            "expired": [],
            "violations": [],
            "errors": errors,
            "scan": scan,
            "projected_count": len(remaining),
            "projected_bytes": remaining_bytes,
        }
        after_count = len(remaining)
        after_bytes = remaining_bytes
    else:
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
        after_scan = after.get("scan") if isinstance(after.get("scan"), dict) else {}
        if after_scan.get("complete") is not True:
            errors.extend(str(item) for item in after.get("errors") or [])
            errors.append("post_scan_incomplete")
            # Destructive work already happened. Preserve exact evidence using
            # successful unlink totals rather than an incomplete post-scan.
            after_count = max(0, before_count - len(removed))
            after_bytes = max(0, before_bytes - removed_bytes)
        else:
            after_count = int(after.get("count", 0))
            after_bytes = int(after.get("bytes", 0))
    removed_names = set(removed)
    reason_counts: dict[str, int] = {}
    for path, reasons in selected_reasons.items():
        if path.name not in removed_names:
            continue
        for reason in reasons:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    loss = finalize_event(
        root,
        loss_event(
            domain=accounting_domain,
            operation=Path(directory).relative_to(root).as_posix(),
            applied=not dry_run and bool(removed) and not errors,
            dry_run=dry_run,
            files_before=before_count,
            files_after=after_count,
            bytes_before=before_bytes,
            bytes_after=after_bytes,
            reasons=reason_counts,
            errors=errors,
            examples=removed,
        ),
    )
    accounting_ok = loss.get("accounting", {}).get("ok") is True
    return {
        "ok": bool(after.get("ok")) and not errors and accounting_ok,
        "dry_run": dry_run,
        "removed": removed,
        "removed_count": len(removed),
        "removed_bytes": removed_bytes,
        "errors": errors,
        "status": after,
        "scan": scan,
        "loss": loss,
    }


__all__ = ["prune_directory", "retention_status"]
