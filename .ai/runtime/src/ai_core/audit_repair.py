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
from pathlib import Path
from typing import Any

from .memory import (
    _audit_file_sort_key,
    all_audit_files,
    audit_segment_sequence_issues,
    audit_transaction_lock_path,
    jsonl_lock_path,
)
from .private_write import (
    atomic_write_private_text,
    private_file_lock,
    read_root_confined_text,
    rename_root_confined_regular_file,
)


def _line_sha(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def _sequence_failure(issues: list[dict[str, Any]], *, root: Path) -> dict[str, Any]:
    first = issues[0]
    kind = str(first.get("kind") or "invalid")
    if kind == "duplicate":
        def display_path(value: object) -> str:
            candidate = Path(str(value))
            try:
                return candidate.relative_to(root).as_posix()
            except ValueError:
                return candidate.as_posix()

        paths = sorted(
            display_path(path)
            for issue in issues
            if issue.get("kind") == "duplicate"
            for path in issue.get("paths", [])
        )
        error = "duplicate audit segment sequence requires explicit divergence resolution"
    else:
        paths = [f".ai/memory/audit/{int(first.get('year', 0))}"]
        error = "audit segment sequence gap requires restoring the missing raw segment"
    return {
        "ok": False,
        "error": error,
        "files": [{"path": path, "skipped": f"segment_sequence_{kind}"} for path in paths],
        "total_repaired": 0,
        "errors": paths,
    }


def _find_first_mismatch(lines: list[str]) -> int | None:
    """Return index of the first chained record whose prev_sha is wrong, or None."""
    prev_line_text: str | None = None
    for idx, ln in enumerate(lines):
        if not ln.strip():
            continue
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            prev_line_text = ln
            continue
        if isinstance(rec, dict) and "prev_sha" in rec:
            expected = None if prev_line_text is None else _line_sha(prev_line_text)
            if rec.get("prev_sha") != expected:
                return idx
        prev_line_text = ln
    return None


def _rewrite_chain(lines: list[str], start_idx: int) -> tuple[list[str], int]:
    """Rewrite prev_sha for every chained record from start_idx onward.

    Returns (new_lines, repaired_count).
    """
    out: list[str] = list(lines[:start_idx])
    prev: str | None = lines[start_idx - 1] if start_idx > 0 else None
    repaired = 0
    for ln in lines[start_idx:]:
        if not ln.strip():
            out.append(ln)
            continue
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            out.append(ln)
            prev = ln
            continue
        if isinstance(rec, dict) and "prev_sha" in rec:
            rec["prev_sha"] = _line_sha(prev) if prev is not None else None
            new_ln = json.dumps(
                rec, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            out.append(new_ln)
            prev = new_ln
            repaired += 1
        else:
            out.append(ln)
            prev = ln
    return out, repaired


def repair_audit_chain(root: Path, *, year: int | None = None) -> dict[str, Any]:
    """Repair per-file chains and lossless segment-link markers in place.

    When ``year`` is None, repair every year file under the audit directory.
    Safe to call when the chain is already intact (returns ``repaired=0``).

    Returns ``{"ok": bool, "files": [{"path", "first_mismatch", "repaired"}], "total_repaired": int}``.
    """
    audit_dir = root / ".ai" / "memory" / "audit"
    if not audit_dir.is_dir():
        return {"ok": False, "error": "audit dir missing", "files": [], "total_repaired": 0}

    candidates = [
        path
        for path in all_audit_files(root)
        if year is None or (_audit_file_sort_key(path.name) or (-1, -1, -1))[0] == year
    ]
    if year is not None and not candidates:
        candidates = [audit_dir / f"{year}.jsonl"]

    sequence_issues = audit_segment_sequence_issues(candidates)
    if sequence_issues:
        return _sequence_failure(sequence_issues, root=root)

    files: list[dict[str, Any]] = []
    total = 0
    errors: list[str] = []
    previous_year: int | None = None
    previous_rel: str | None = None
    previous_last_sha: str | None = None
    previous_file_sha256: str | None = None
    previous_file_bytes: int | None = None
    with private_file_lock(audit_transaction_lock_path(root), root=root):
        # Repeat discovery and all preflight checks under the same transaction
        # lock used by append/rotation. No file is rewritten until the complete
        # candidate set is readable and its sequence/lineage is lossless.
        candidates = [
            path
            for path in all_audit_files(root)
            if year is None or (_audit_file_sort_key(path.name) or (-1, -1, -1))[0] == year
        ]
        sequence_issues = audit_segment_sequence_issues(candidates)
        if sequence_issues:
            return _sequence_failure(sequence_issues, root=root)
        preflight_text: dict[Path, str] = {}
        first_seen_years: set[int] = set()
        for candidate in candidates:
            rel = candidate.relative_to(root).as_posix()
            try:
                candidate_text, _candidate_state = read_root_confined_text(
                    candidate,
                    root=root,
                    max_bytes=100_000_000,
                    require_private=False,
                    require_owner=True,
                    reject_group_other_writable=True,
                )
            except (OSError, UnicodeDecodeError) as exc:
                return {
                    "ok": False,
                    "error": f"audit preflight failed: {type(exc).__name__}",
                    "files": [{"path": rel, "skipped": "read_error"}],
                    "total_repaired": 0,
                    "errors": [rel],
                }
            preflight_text[candidate] = candidate_text
            key = _audit_file_sort_key(candidate.name)
            candidate_year = key[0] if key is not None else None
            if candidate_year is None or candidate_year in first_seen_years:
                continue
            first_seen_years.add(candidate_year)
            first_line = next((line for line in candidate_text.splitlines() if line.strip()), None)
            try:
                first_record = json.loads(first_line) if first_line is not None else None
            except json.JSONDecodeError:
                first_record = None
            if isinstance(first_record, dict) and first_record.get("action") == "audit.segment_started":
                return {
                    "ok": False,
                    "error": "orphan audit segment marker requires restoring the missing raw segment",
                    "files": [{"path": rel, "skipped": "segment_link_orphan"}],
                    "total_repaired": 0,
                    "errors": [rel],
                }
        for path in candidates:
            rel = path.relative_to(root).as_posix()
            if not path.exists():
                files.append({"path": rel, "skipped": "missing"})
                continue
            text = preflight_text[path]
            lines = text.splitlines()
            mismatch_at = _find_first_mismatch(lines)
            key = _audit_file_sort_key(path.name)
            current_year = key[0] if key is not None else None
            boundary_changed = False
            if previous_year is not None and current_year == previous_year:
                first_idx = next((idx for idx, line in enumerate(lines) if line.strip()), None)
                try:
                    first = json.loads(lines[first_idx]) if first_idx is not None else None
                except json.JSONDecodeError:
                    first = None
                if not isinstance(first, dict) or first.get("action") != "audit.segment_started":
                    files.append({"path": rel, "skipped": "segment_link_marker_missing"})
                    errors.append(rel)
                    continue
                payload = first.get("payload") if isinstance(first.get("payload"), dict) else {}
                expected_payload = dict(payload)
                expected_payload.update(
                    {
                        "previous_segment": previous_rel,
                        "previous_file_sha256": previous_file_sha256,
                        "previous_last_sha": previous_last_sha,
                        "bytes_segmented": previous_file_bytes,
                        "lossy": False,
                    }
                )
                if expected_payload != payload:
                    first["payload"] = expected_payload
                    lines[first_idx] = json.dumps(
                        first, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    )
                    mismatch_at = first_idx if mismatch_at is None else min(mismatch_at, first_idx)
                    boundary_changed = True

            entry: dict[str, Any] = {"path": rel}
            if mismatch_at is None:
                new_lines = lines
                repaired = 0
                entry["first_mismatch"] = None
            else:
                new_lines, repaired = _rewrite_chain(lines, mismatch_at)
                entry["first_mismatch"] = mismatch_at + 1
            if mismatch_at is not None or boundary_changed:
                new_text = "\n".join(new_lines) + ("\n" if text.endswith("\n") else "")
                with private_file_lock(jsonl_lock_path(path), root=root):
                    atomic_write_private_text(path, new_text, root=root)
                text = new_text
                total += repaired
            if key is not None and key[1] == 0:
                canonical_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
                canonical_path = path.with_name(
                    f"{key[0]}.{key[2]:06d}.{canonical_digest}.jsonl"
                )
                if canonical_path != path:
                    old_rel = rel
                    with private_file_lock(jsonl_lock_path(path), root=root):
                        rename_root_confined_regular_file(path, canonical_path, root=root)
                    path = canonical_path
                    rel = path.relative_to(root).as_posix()
                    entry["path"] = rel
                    entry["renamed_from"] = old_rel
            entry["repaired"] = repaired
            files.append(entry)

            nonempty = [line for line in text.splitlines() if line.strip()]
            previous_year = current_year
            previous_rel = rel
            previous_last_sha = _line_sha(nonempty[-1]) if nonempty else None
            previous_file_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
            previous_file_bytes = len(text.encode("utf-8"))

    return {"ok": not errors, "files": files, "total_repaired": total, "errors": errors}
