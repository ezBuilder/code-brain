"""Repair the prev_sha hash chain in audit/*.jsonl after stash/merge artifacts.

The audit log is append-only and chained by SHA-256 of the previous line.
Operations that splice external content into the file (git stash union
merges, manual edits, partial restore) can produce a single mismatch row
whose `prev_sha` no longer matches its predecessor. Doctor flags this as
``audit_chain invalid`` and refuses to certify a strict pass.

This module provides a pure, deterministic repair: walk the file, find the
first mismatch, then rewrite every chained record from that index onward
so that each row's ``prev_sha`` equals SHA-256 of the line immediately
above it. No content is dropped; only the ``prev_sha`` field of mis-
chained records is recomputed.

Used both by the CLI command ``ai audit repair-chain`` and as the body of
any future auto-repair hook.
"""
from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from typing import Any, Iterator

from .memory import (
    AUDIT_LINE_MAX_BYTES,
    AUDIT_MAX_BYTES,
    all_audit_files,
    jsonl_lock_path,
)
from .private_write import (
    atomic_write_private_lines,
    open_root_confined_binary,
    private_file_lock,
)


AUDIT_REPAIR_MAX_RECORDS = 250_000


def _line_sha(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def _iter_audit_lines(path: Path, *, root: Path) -> Iterator[tuple[int, str, object | None]]:
    line_count = 0
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
            line_count += 1
            if line_count > int(AUDIT_REPAIR_MAX_RECORDS):
                raise OSError(
                    f"audit repair record limit exceeded: "
                    f"{line_count}>{AUDIT_REPAIR_MAX_RECORDS}"
                )
            if len(raw) > int(AUDIT_LINE_MAX_BYTES):
                while raw and not raw.endswith(b"\n"):
                    raw = handle.readline(64 * 1024)
                raise OSError(f"audit line exceeds {AUDIT_LINE_MAX_BYTES} bytes")
            try:
                line = raw.decode("utf-8", errors="strict").rstrip("\r\n")
            except UnicodeDecodeError as exc:
                raise OSError("audit line is not valid UTF-8") from exc
            if not line.strip():
                yield line_count, line, None
                continue
            try:
                record: object | None = json.loads(line)
            except json.JSONDecodeError:
                record = None
            yield line_count, line, record


def _scan_first_mismatch(path: Path, *, root: Path) -> tuple[int | None, bool]:
    previous: str | None = None
    mismatch: int | None = None
    for line_no, line, record in _iter_audit_lines(path, root=root):
        if not line.strip():
            continue
        if isinstance(record, dict) and "prev_sha" in record:
            expected = None if previous is None else _line_sha(previous)
            if record.get("prev_sha") != expected and mismatch is None:
                mismatch = line_no
        previous = line

    with open_root_confined_binary(
        path,
        root=root,
        max_bytes=AUDIT_MAX_BYTES,
        require_private=False,
    ) as (handle, state):
        trailing_newline = False
        if int(state.st_size) > 0:
            handle.seek(-1, 2)
            trailing_newline = handle.read(1) in {b"\n", b"\r"}
    return mismatch, trailing_newline


def _repair_lines(
    path: Path,
    *,
    root: Path,
    start_line: int,
    trailing_newline: bool,
    counter: dict[str, int],
) -> Iterator[str]:
    previous: str | None = None
    pending: str | None = None
    for line_no, line, record in _iter_audit_lines(path, root=root):
        rendered = line
        if line.strip():
            if line_no >= start_line and isinstance(record, dict) and "prev_sha" in record:
                updated = dict(record)
                updated["prev_sha"] = _line_sha(previous) if previous is not None else None
                rendered = json.dumps(
                    updated,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                counter["repaired"] = int(counter.get("repaired", 0)) + 1
            previous = rendered
        if pending is not None:
            yield pending + "\n"
        pending = rendered
    if pending is not None:
        yield pending + ("\n" if trailing_newline else "")


def repair_audit_chain(root: Path, *, year: int | None = None) -> dict[str, Any]:
    """Repair the prev_sha chain in .ai/memory/audit/<year>.jsonl in place.

    When ``year`` is None, repair every year file under the audit directory.
    Safe to call when the chain is already intact (returns ``repaired=0``).

    Returns ``{"ok": bool, "files": [{"path", "first_mismatch", "repaired"}], "total_repaired": int}``.
    """
    audit_dir = root / ".ai" / "memory" / "audit"
    try:
        audit_state = audit_dir.lstat()
    except OSError:
        return {"ok": False, "error": "audit dir missing", "files": [], "total_repaired": 0}
    if stat.S_ISLNK(audit_state.st_mode) or not stat.S_ISDIR(audit_state.st_mode):
        return {"ok": False, "error": "audit dir untrusted", "files": [], "total_repaired": 0}

    if year is not None:
        candidates = [audit_dir / f"{year}.jsonl"]
    else:
        candidates = all_audit_files(root)

    files: list[dict[str, Any]] = []
    total = 0
    errors: list[str] = []
    for path in candidates:
        try:
            path_state = path.lstat()
        except FileNotFoundError:
            files.append({"path": path.relative_to(root).as_posix(), "skipped": "missing"})
            continue
        except OSError as exc:
            detail = f"read_error:{exc}"
            files.append({"path": path.relative_to(root).as_posix(), "skipped": detail})
            errors.append(f"{path.relative_to(root).as_posix()}:{detail}")
            continue
        if (
            stat.S_ISLNK(path_state.st_mode)
            or not stat.S_ISREG(path_state.st_mode)
            or int(getattr(path_state, "st_nlink", 1)) != 1
        ):
            detail = "untrusted_file"
            files.append({"path": path.relative_to(root).as_posix(), "skipped": detail})
            errors.append(f"{path.relative_to(root).as_posix()}:{detail}")
            continue
        try:
            with private_file_lock(jsonl_lock_path(path), root=root):
                mismatch_at, trailing_newline = _scan_first_mismatch(path, root=root)
                entry: dict[str, Any] = {"path": path.relative_to(root).as_posix()}
                if mismatch_at is None:
                    entry["first_mismatch"] = None
                    entry["repaired"] = 0
                else:
                    counter = {"repaired": 0}
                    atomic_write_private_lines(
                        path,
                        _repair_lines(
                            path,
                            root=root,
                            start_line=mismatch_at,
                            trailing_newline=trailing_newline,
                            counter=counter,
                        ),
                        root=root,
                        max_bytes=AUDIT_MAX_BYTES,
                    )
                    repaired = int(counter["repaired"])
                    entry["first_mismatch"] = mismatch_at
                    entry["repaired"] = repaired
                    total += repaired
                files.append(entry)
        except OSError as exc:
            detail = f"repair_error:{exc}"
            files.append({"path": path.relative_to(root).as_posix(), "skipped": detail})
            errors.append(f"{path.relative_to(root).as_posix()}:{detail}")

    return {
        "ok": not errors,
        "files": files,
        "total_repaired": total,
        "errors": errors,
    }
