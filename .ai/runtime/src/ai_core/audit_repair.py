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


def _line_sha(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def _find_first_mismatch(lines: list[str]) -> int | None:
    """Return index of the first chained record whose prev_sha is wrong, or None."""
    prev_line_text: str | None = None
    for idx, ln in enumerate(lines):
        if not ln.strip():
            continue
        if prev_line_text is None:
            prev_line_text = ln
            continue
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            prev_line_text = ln
            continue
        if isinstance(rec, dict) and "prev_sha" in rec:
            expected = _line_sha(prev_line_text)
            if rec.get("prev_sha") != expected:
                return idx
        prev_line_text = ln
    return None


def _rewrite_chain(lines: list[str], start_idx: int) -> tuple[list[str], int]:
    """Rewrite prev_sha for every chained record from start_idx onward.

    Returns (new_lines, repaired_count).
    """
    out: list[str] = list(lines[:start_idx])
    prev = lines[start_idx - 1] if start_idx > 0 else ""
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
            rec["prev_sha"] = _line_sha(prev)
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
    """Repair the prev_sha chain in .ai/memory/audit/<year>.jsonl in place.

    When ``year`` is None, repair every year file under the audit directory.
    Safe to call when the chain is already intact (returns ``repaired=0``).

    Returns ``{"ok": bool, "files": [{"path", "first_mismatch", "repaired"}], "total_repaired": int}``.
    """
    audit_dir = root / ".ai" / "memory" / "audit"
    if not audit_dir.is_dir():
        return {"ok": False, "error": "audit dir missing", "files": [], "total_repaired": 0}

    if year is not None:
        candidates = [audit_dir / f"{year}.jsonl"]
    else:
        candidates = sorted(audit_dir.glob("*.jsonl"))

    files: list[dict[str, Any]] = []
    total = 0
    for path in candidates:
        if not path.exists():
            files.append({"path": path.relative_to(root).as_posix(), "skipped": "missing"})
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            files.append({"path": path.relative_to(root).as_posix(), "skipped": f"read_error:{exc}"})
            continue
        lines = text.splitlines()
        mismatch_at = _find_first_mismatch(lines)
        entry: dict[str, Any] = {"path": path.relative_to(root).as_posix()}
        if mismatch_at is None:
            entry["first_mismatch"] = None
            entry["repaired"] = 0
        else:
            new_lines, repaired = _rewrite_chain(lines, mismatch_at)
            path.write_text("\n".join(new_lines) + ("\n" if text.endswith("\n") else ""), encoding="utf-8")
            entry["first_mismatch"] = mismatch_at + 1  # human 1-based
            entry["repaired"] = repaired
            total += repaired
        files.append(entry)

    return {"ok": True, "files": files, "total_repaired": total}
