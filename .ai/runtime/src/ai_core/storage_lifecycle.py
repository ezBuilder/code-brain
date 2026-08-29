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
EPISODIC_MAX_ENTRIES = 8
EPISODIC_MAX_TOTAL_BYTES = 128 * 1024 * 1024
AUDIT_ROLLUP_MAX_ENTRIES = 16
AUDIT_ROLLUP_MAX_TOTAL_BYTES = 64 * 1024 * 1024
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


def _pinned_accounting(root: Path, directory: Path) -> tuple[int, int]:
    """Return pinned bytes and top-level entry count with one managed scan."""
    try:
        rows, _errors = _managed_entries(root, directory)
    except Exception:
        return 0, 0
    return (
        sum(int(row["bytes"]) for row in rows if bool(row["pinned"])),
        sum(1 for row in rows if bool(row["pinned"])),
    )


def _pinned_bytes(root: Path, directory: Path) -> int:
    """Bytes held by entries the enforcer is not allowed to delete.

    Pinning is a deletion veto (tracked in git, referenced by tracked source, or an
    explicit ``.keep``). Counting those bytes against the cap made the cap unsatisfiable:
    measured on blurivo, .ai/tmp held 546MB of which 475MB were three user fixtures with
    explicit .keep markers, so every enforce run deleted what it could and still reported
    failure — a permanent red doctor that no amount of cleanup could clear. Deliberately
    kept bytes are the user's choice, so they are excluded from the *limit* while still
    being reported.
    """
    return _pinned_accounting(root, directory)[0]


def _pinned_entries(root: Path, directory: Path) -> int:
    """Top-level entries explicitly outside automatic count quotas."""

    return _pinned_accounting(root, directory)[1]


def workspace_storage_status(root: Path) -> dict[str, int | bool]:
    root = Path(root)
    ai = _tree_usage(root / ".ai")
    tmp = _tree_usage(root / ".ai" / "tmp")
    outputs = _tree_usage(root / ".ai" / "outputs")
    memory = _tree_usage(root / ".ai" / "memory")
    episodic = _tree_usage(root / ".ai" / "memory" / "episodic")
    audit_rollups = _tree_usage(root / ".ai" / "memory" / "audit-rollups")
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
    try:
        episodic_top_entries = len(
            list_root_confined_directory(
                root / ".ai" / "memory" / "episodic",
                root=root,
                max_entries=_DIRECTORY_SCAN_MAX_ENTRIES,
            )
        )
    except FileNotFoundError:
        episodic_top_entries = 0
    except OSError:
        episodic_top_entries = EPISODIC_MAX_ENTRIES + 1
    try:
        audit_rollup_top_entries = len(
            list_root_confined_directory(
                root / ".ai" / "memory" / "audit-rollups",
                root=root,
                max_entries=_DIRECTORY_SCAN_MAX_ENTRIES,
            )
        )
    except FileNotFoundError:
        audit_rollup_top_entries = 0
    except OSError:
        audit_rollup_top_entries = AUDIT_ROLLUP_MAX_ENTRIES + 1
    complete = bool(
        ai["complete"]
        and tmp["complete"]
        and outputs["complete"]
        and memory["complete"]
        and episodic["complete"]
        and audit_rollups["complete"]
    )
    errors = (
        int(ai["errors"])
        + int(tmp["errors"])
        + int(outputs["errors"])
        + int(memory["errors"])
        + int(episodic["errors"])
        + int(audit_rollups["errors"])
    )
    ai_bytes = int(ai["bytes"])
    tmp_bytes = int(tmp["bytes"])
    output_bytes = int(outputs["bytes"])
    memory_bytes = int(memory["bytes"])
    episodic_bytes = int(episodic["bytes"])
    audit_rollup_bytes = int(audit_rollups["bytes"])
    authoritative_memory_bytes = max(
        0, memory_bytes - episodic_bytes - audit_rollup_bytes
    )
    tmp_pinned, tmp_pinned_entries = _pinned_accounting(root, root / ".ai" / "tmp")
    output_pinned, output_pinned_entries = _pinned_accounting(root, root / ".ai" / "outputs")
    episodic_pinned, episodic_pinned_entries = _pinned_accounting(
        root, root / ".ai" / "memory" / "episodic"
    )
    audit_rollup_pinned, audit_rollup_pinned_entries = _pinned_accounting(
        root, root / ".ai" / "memory" / "audit-rollups"
    )
    # Reclaimable = what the enforcer could actually delete. The caps apply to this.
    tmp_reclaimable = max(0, tmp_bytes - tmp_pinned)
    output_reclaimable = max(0, output_bytes - output_pinned)
    episodic_reclaimable = max(0, episodic_bytes - episodic_pinned)
    audit_rollup_reclaimable = max(0, audit_rollup_bytes - audit_rollup_pinned)
    tmp_reclaimable_entries = max(0, tmp_top_entries - tmp_pinned_entries)
    output_reclaimable_entries = max(0, output_top_entries - output_pinned_entries)
    episodic_reclaimable_entries = max(0, episodic_top_entries - episodic_pinned_entries)
    audit_rollup_reclaimable_entries = max(
        0, audit_rollup_top_entries - audit_rollup_pinned_entries
    )
    # Raw audit/decision/session memory is authoritative and has no safe automatic
    # deletion path. Counting it against an automatically enforced cap makes that
    # cap permanently unsatisfiable once a long-lived project crosses the limit.
    # The episodic pyramid is derived and disposable, so it remains bounded below.
    ai_reclaimable = max(
        0,
        ai_bytes
        - authoritative_memory_bytes
        - tmp_pinned
        - output_pinned
        - episodic_pinned
        - audit_rollup_pinned,
    )
    return {
        "ok": complete
        and errors == 0
        and ai_reclaimable <= AI_MAX_TOTAL_BYTES
        and tmp_reclaimable <= TMP_MAX_TOTAL_BYTES
        and tmp_reclaimable_entries <= TMP_MAX_ENTRIES
        and output_reclaimable <= OUTPUT_MAX_TOTAL_BYTES
        and output_reclaimable_entries <= OUTPUT_MAX_ENTRIES
        and episodic_reclaimable <= EPISODIC_MAX_TOTAL_BYTES
        and episodic_reclaimable_entries <= EPISODIC_MAX_ENTRIES
        and audit_rollup_reclaimable <= AUDIT_ROLLUP_MAX_TOTAL_BYTES
        and audit_rollup_reclaimable_entries <= AUDIT_ROLLUP_MAX_ENTRIES,
        "complete": complete,
        "errors": errors,
        "ai_bytes": ai_bytes,
        "ai_max_bytes": AI_MAX_TOTAL_BYTES,
        "ai_reclaimable_bytes": ai_reclaimable,
        "authoritative_memory_bytes": authoritative_memory_bytes,
        "episodic_bytes": episodic_bytes,
        "episodic_max_bytes": EPISODIC_MAX_TOTAL_BYTES,
        "episodic_pinned_bytes": episodic_pinned,
        "episodic_reclaimable_bytes": episodic_reclaimable,
        "episodic_top_entries": episodic_top_entries,
        "episodic_reclaimable_entries": episodic_reclaimable_entries,
        "episodic_max_entries": EPISODIC_MAX_ENTRIES,
        "audit_rollup_bytes": audit_rollup_bytes,
        "audit_rollup_max_bytes": AUDIT_ROLLUP_MAX_TOTAL_BYTES,
        "audit_rollup_pinned_bytes": audit_rollup_pinned,
        "audit_rollup_reclaimable_entries": audit_rollup_reclaimable_entries,
        "audit_rollup_reclaimable_bytes": audit_rollup_reclaimable,
        "audit_rollup_top_entries": audit_rollup_top_entries,
        "audit_rollup_max_entries": AUDIT_ROLLUP_MAX_ENTRIES,
        "tmp_bytes": tmp_bytes,
        "tmp_max_bytes": TMP_MAX_TOTAL_BYTES,
        "tmp_pinned_bytes": tmp_pinned,
        "tmp_reclaimable_bytes": tmp_reclaimable,
        "tmp_top_entries": tmp_top_entries,
        "tmp_reclaimable_entries": tmp_reclaimable_entries,
        "tmp_max_entries": TMP_MAX_ENTRIES,
        "output_bytes": output_bytes,
        "output_max_bytes": OUTPUT_MAX_TOTAL_BYTES,
        "output_pinned_bytes": output_pinned,
        "output_reclaimable_bytes": output_reclaimable,
        "output_top_entries": output_top_entries,
        "output_reclaimable_entries": output_reclaimable_entries,
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


# Cap the referenced-name scan so a huge repository cannot slow enforcement.
_REFERENCE_SCAN_MAX_BYTES = 8 * 1024 * 1024
_REFERENCE_SCAN_MAX_FILES = 4_000


def _referenced_entry_names(root: Path, directory: Path, names: list[str]) -> set[str]:
    """Names under ``directory`` that tracked source text mentions by name.

    Motivation: enforcement deleted `.ai/tmp/<fixture>.mp4`, a 201MB SHA-256-pinned
    test fixture that tracked scripts and an integration test referenced by name.
    Nothing about size or age distinguished it from disposable scratch, and the
    only protection was a hand-placed `.keep` marker nobody had added, so a
    routine cap enforcement silently broke a live verification gate.

    Conservative by construction: substring match on tracked text only, so a false
    positive merely retains a file. Any failure returns an empty set, leaving the
    pre-existing keep-marker and git-tracked rules as the sole protection.
    """
    candidates = {name for name in names if len(name) >= 8 and "." in name}
    if not candidates:
        return set()
    try:
        listing = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    if listing.returncode != 0:
        return set()

    try:
        skip_prefix = directory.relative_to(root).as_posix()
    except ValueError:
        skip_prefix = None

    referenced: set[str] = set()
    scanned = 0
    for raw in listing.stdout.split(b"\0"):
        if not raw or scanned >= _REFERENCE_SCAN_MAX_FILES:
            break
        try:
            rel = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        # Never let the managed directory vouch for its own contents.
        if skip_prefix and (rel == skip_prefix or rel.startswith(skip_prefix + "/")):
            continue
        target = root / rel
        try:
            if target.is_symlink() or not target.is_file():
                continue
            if target.stat().st_size > _REFERENCE_SCAN_MAX_BYTES:
                continue
            text = target.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        scanned += 1
        for name in candidates - referenced:
            if name in text:
                referenced.add(name)
        if referenced == candidates:
            break
    return referenced


def _managed_entries(root: Path, directory: Path) -> tuple[list[dict[str, object]], int]:
    try:
        names = list_root_confined_directory(directory, root=root, max_entries=_DIRECTORY_SCAN_MAX_ENTRIES)
    except FileNotFoundError:
        return [], 0
    except OSError:
        return [], 1
    tracked, tracked_known = _tracked_top_entries(root, directory)
    referenced = _referenced_entry_names(root, directory, names)
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
                # Two distinct reasons an entry survives, deliberately kept apart:
                #  * `pinned` — an explicit user/repo decision (tracked in git, referenced
                #    by tracked source, or a `.keep` marker). These bytes are excluded from
                #    the cap, because the user chose to keep them.
                #  * `undetermined` — git could not be consulted (no repo, git missing,
                #    oversized listing), so deletion is withheld out of caution. These bytes
                #    MUST still count against the cap; treating a failed lookup as a user
                #    decision would silently disable quota enforcement for every
                #    non-git workspace.
                "pinned": (
                    name in tracked
                    or name in referenced
                    or _has_keep_marker(path)
                ),
                "undetermined": not tracked_known,
            }
        )
    return rows, errors


def _protected(row: dict[str, object], *, allow_undetermined: bool = False) -> bool:
    """Return whether an entry is outside the caller's deletion authority.

    Unknown git state remains protected for general scratch/output data. The two
    explicitly derived roots may opt in to reclaiming it: every top-level entry
    there is disposable by contract and can be rebuilt from authoritative audit.
    Explicit pins (tracked, referenced, or ``.keep``) always win.
    """

    return bool(row.get("pinned")) or (
        bool(row.get("undetermined")) and not allow_undetermined
    )


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
    allow_undetermined: bool = False,
) -> dict[str, int | bool]:
    rows, errors = _managed_entries(root, directory)
    bytes_before = sum(int(row["bytes"]) for row in rows)
    removed = 0
    removed_bytes = 0
    cutoff_ns = time.time_ns() - max(0, int(keep_days or 0)) * 86_400 * 1_000_000_000
    survivors: list[dict[str, object]] = []
    for row in sorted(rows, key=lambda value: (int(value["mtime_ns"]), str(value["name"]))):
        expired = keep_days is not None and int(row["mtime_ns"]) < cutoff_ns
        if expired and not _protected(row, allow_undetermined=allow_undetermined):
            if _remove_managed_entry(Path(row["path"]), root=root):
                removed += 1
                removed_bytes += int(row["bytes"])
                continue
            errors += 1
        survivors.append(row)

    total = sum(int(row["bytes"]) for row in survivors)
    count = len(survivors)
    reclaimable_total = sum(int(row["bytes"]) for row in survivors if not row["pinned"])
    reclaimable_count = sum(1 for row in survivors if not row["pinned"])
    kept: list[dict[str, object]] = []
    for row in survivors:
        over = (
            reclaimable_count > max(0, int(max_entries))
            or reclaimable_total > max(0, int(max_total_bytes))
        )
        if over and not _protected(row, allow_undetermined=allow_undetermined):
            if _remove_managed_entry(Path(row["path"]), root=root):
                removed += 1
                removed_bytes += int(row["bytes"])
                total -= int(row["bytes"])
                count -= 1
                if not row["pinned"]:
                    reclaimable_total -= int(row["bytes"])
                    reclaimable_count -= 1
                continue
            errors += 1
        kept.append(row)

    # Judge the enforcer on what it could delete, not on what it is forbidden to touch.
    # Pinned entries are a user decision (tracked / referenced by source / explicit .keep);
    # counting them made `ok` unreachable and left doctor permanently red even after a
    # successful sweep (blurivo: 475MB of pinned fixtures in a 512MB cap).
    pinned_bytes = sum(int(row["bytes"]) for row in kept if row["pinned"])
    reclaimable = max(0, reclaimable_total)
    return {
        "ok": (
            errors == 0
            and reclaimable <= max_total_bytes
            and reclaimable_count <= max_entries
        ),
        "removed": removed,
        "removed_bytes": removed_bytes,
        "bytes_before": bytes_before,
        "bytes_kept": total,
        "bytes_pinned": pinned_bytes,
        "bytes_reclaimable": reclaimable,
        "kept": count,
        "reclaimable_entries": reclaimable_count,
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
        if _protected(row):
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
    episodic = _prune_managed_directory(
        root,
        root / ".ai" / "memory" / "episodic",
        keep_days=None,
        max_entries=EPISODIC_MAX_ENTRIES,
        max_total_bytes=EPISODIC_MAX_TOTAL_BYTES,
        allow_undetermined=True,
    )
    audit_rollups = _prune_managed_directory(
        root,
        root / ".ai" / "memory" / "audit-rollups",
        keep_days=None,
        max_entries=AUDIT_ROLLUP_MAX_ENTRIES,
        max_total_bytes=AUDIT_ROLLUP_MAX_TOTAL_BYTES,
        allow_undetermined=True,
    )
    status = workspace_storage_status(root)
    reclaim: list[dict[str, object]] = []
    excess = max(0, int(status["ai_reclaimable_bytes"]) - AI_MAX_TOTAL_BYTES)
    for directory in (root / ".ai" / "tmp", root / ".ai" / "outputs"):
        if excess <= 0:
            break
        result = _reclaim_from_directory(root, directory, needed_bytes=excess)
        reclaim.append({"directory": directory.relative_to(root).as_posix(), **result})
        excess = max(0, excess - int(result["reclaimed_bytes"]))
    if reclaim:
        status = workspace_storage_status(root)
    return {
        "ok": bool(
            tmp["ok"]
            and outputs["ok"]
            and episodic["ok"]
            and audit_rollups["ok"]
            and status["ok"]
        ),
        "tmp": tmp,
        "outputs": outputs,
        "episodic": episodic,
        "audit_rollups": audit_rollups,
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
