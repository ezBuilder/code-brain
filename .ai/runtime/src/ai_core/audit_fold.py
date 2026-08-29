"""Build private, immutable rollups for cold audit records.

The audit JSONL is the raw source of truth. Folding therefore never rewrites
or deletes it; rollups live in a separate private sidecar and retain source
file/line/hash anchors. ``_folded`` rows written by older runtimes remain
read-compatible and are ignored as already-derived records.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .memory import all_audit_files, audit_transaction_lock_path, jsonl_lock_path, line_sha, read_state_text
from .private_write import atomic_write_private_text, private_file_lock

_MAX_EVENT_ID_ANCHORS = 32


def _parse_ts(ts_str: str) -> datetime | None:
    """Parse ISO timestamp to UTC datetime, or None if invalid."""
    try:
        ts_str_clean = ts_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts_str_clean)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError, TypeError):
        return None


def _date_from_ts(ts: datetime) -> str:
    return ts.date().isoformat()


def audit_rollup_path(root: Path, audit_path: Path) -> Path:
    """Return the per-source private rollup sidecar path."""
    return Path(root) / ".ai" / "memory" / "audit-rollups" / audit_path.name


def _compress_line_ranges(lines: list[int]) -> list[list[int]]:
    ranges: list[list[int]] = []
    for line_no in sorted(lines):
        if ranges and line_no == ranges[-1][1] + 1:
            ranges[-1][1] = line_no
        else:
            ranges.append([line_no, line_no])
    return ranges


def _rollup_id(source: str, date_key: str, entries: list[dict[str, Any]]) -> str:
    material = "\n".join(
        f"{entry['line_no']}:{entry['event_id'] or entry['line_sha']}" for entry in entries
    )
    digest = hashlib.sha256(f"{source}\n{date_key}\n{material}".encode("utf-8")).hexdigest()
    return f"rollup-{digest}"


def _bounded_event_anchors(event_ids: list[str]) -> list[str]:
    if len(event_ids) <= _MAX_EVENT_ID_ANCHORS:
        return event_ids
    positions = {
        round(index * (len(event_ids) - 1) / (_MAX_EVENT_ID_ANCHORS - 1))
        for index in range(_MAX_EVENT_ID_ANCHORS)
    }
    return [event_ids[index] for index in sorted(positions)]


def _make_rollup(*, source: str, date_key: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for entry in entries:
        action = str(entry["record"].get("action") or "_unknown")
        counts[action] = counts.get(action, 0) + 1
    line_numbers = [int(entry["line_no"]) for entry in entries]
    event_ids = [entry["event_id"] for entry in entries if entry["event_id"]]
    raw_digest = hashlib.sha256(
        "\n".join(str(entry["line"]) for entry in entries).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": 1,
        "rollup_id": _rollup_id(source, date_key, entries),
        "kind": "audit_rollup",
        "ts": f"{date_key}T23:59:59Z",
        "source": {
            "path": source,
            "date": date_key,
            "line_ranges": _compress_line_ranges(line_numbers),
            "event_id_first": event_ids[0] if event_ids else None,
            "event_id_last": event_ids[-1] if event_ids else None,
            "event_id_anchors": _bounded_event_anchors(event_ids),
            "event_id_count": len(event_ids),
            "raw_sha256": raw_digest,
        },
        "payload": {
            "date": date_key,
            "counts": counts,
            "total": len(entries),
            "source_files": [source],
        },
    }


def _rollup_key(record: dict[str, Any]) -> tuple[object, object]:
    source = record.get("source") if isinstance(record.get("source"), dict) else {}
    return source.get("path"), source.get("date")


def _read_rollup_records(path: Path, *, root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    try:
        text = read_state_text(path, max_bytes=100_000_000)
    except FileNotFoundError:
        return [], []
    records: list[dict[str, Any]] = []
    lines: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            lines.append(line)
            continue
        if not isinstance(record, dict):
            lines.append(line)
            continue
        if isinstance(record.get("rollup_id"), str) and record["rollup_id"]:
            records.append(record)
        lines.append(line)
    return records, lines


def _canonical_sidecar_lines(
    existing_records: list[dict[str, Any]],
    existing_lines: list[str],
    candidates: list[dict[str, Any]],
) -> tuple[list[str], int, int]:
    """Replace one canonical source/date record instead of accumulating duplicates."""
    by_key = {_rollup_key(record): record for record in candidates}
    seen: set[tuple[object, object]] = set()
    output: list[str] = []
    for line in existing_lines:
        try:
            loaded = json.loads(line)
        except json.JSONDecodeError:
            output.append(line)
            continue
        if not isinstance(loaded, dict) or not loaded.get("rollup_id"):
            output.append(line)
            continue
        key = _rollup_key(loaded)
        replacement = by_key.get(key)
        if replacement is not None:
            if key in seen:
                continue
            output.append(json.dumps(replacement, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            seen.add(key)
        else:
            if key in seen:
                continue
            output.append(line)
            seen.add(key)
    for key, replacement in by_key.items():
        if key not in seen:
            output.append(json.dumps(replacement, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    existing_by_key = {_rollup_key(record): record for record in existing_records}
    added = sum(1 for key in by_key if key not in existing_by_key)
    updated = sum(
        1
        for key, replacement in by_key.items()
        if key in existing_by_key
        and json.dumps(existing_by_key[key], sort_keys=True, separators=(",", ":"))
        != json.dumps(replacement, sort_keys=True, separators=(",", ":"))
    )
    return output, added, updated


def _fold_one_file(
    root: Path,
    audit_path: Path,
    *,
    cutoff: datetime,
    dry_run: bool,
) -> dict[str, Any]:
    """Read one raw audit file and write only its private rollup sidecar."""
    rel = audit_path.relative_to(root).as_posix()
    cold_entries: dict[str, list[dict[str, Any]]] = {}
    with private_file_lock(jsonl_lock_path(audit_path), root=root):
        audit_text = read_state_text(audit_path, max_bytes=100_000_000)
        for line_no, raw_line in enumerate(audit_text.splitlines(), 1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                loaded = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(loaded, dict) or loaded.get("action") == "_folded":
                continue
            ts = _parse_ts(loaded.get("ts"))
            if ts is None or ts >= cutoff:
                continue
            event_id = loaded.get("event_id")
            cold_entries.setdefault(_date_from_ts(ts), []).append(
                {
                    "line_no": line_no,
                    # Keep the physical row text for provenance.  Parsing may
                    # ignore surrounding whitespace, but drill-down hashes
                    # must describe the bytes actually present in the file.
                    "line": raw_line,
                    "line_sha": line_sha(raw_line),
                    "event_id": event_id if isinstance(event_id, str) else None,
                    "record": loaded,
                }
            )

    sidecar = audit_rollup_path(root, audit_path)
    candidate_records = [
        _make_rollup(source=rel, date_key=date_key, entries=entries)
        for date_key, entries in sorted(cold_entries.items())
    ]
    if dry_run:
        # Do not create the sidecar directory/lock just to inspect a dry run.
        existing_records, existing_lines = _read_rollup_records(sidecar, root=root)
    else:
        with private_file_lock(jsonl_lock_path(sidecar), root=root):
            existing_records, existing_lines = _read_rollup_records(sidecar, root=root)
            output, added_records, updated_records = _canonical_sidecar_lines(
                existing_records, existing_lines, candidate_records
            )
            if output != existing_lines:
                atomic_write_private_text(sidecar, "\n".join(output) + "\n", root=root)
    if dry_run:
        output, added_records, updated_records = _canonical_sidecar_lines(
            existing_records, existing_lines, candidate_records
        )
    changed_records = added_records + updated_records

    source_entries = sum(len(entries) for entries in cold_entries.values())
    return {
        "folded_days": len({record["source"]["date"] for record in candidate_records}) if changed_records else 0,
        "source_entries": source_entries,
        "rolled_up_entries": source_entries,
        # Compatibility field: raw entries are never removed now.
        "removed_entries": 0,
        "added_fold_records": added_records,
        "updated_fold_records": updated_records,
        "touched": (
            f"{sidecar.relative_to(root).as_posix()} (dry_run)" if dry_run else sidecar.relative_to(root).as_posix()
        ) if changed_records else None,
    }


def _fold_files_locked(
    root: Path,
    *,
    cutoff: datetime,
    dry_run: bool,
    result: dict[str, Any],
) -> None:
    for audit_path in all_audit_files(root):
        try:
            folded = _fold_one_file(root, audit_path, cutoff=cutoff, dry_run=dry_run)
        except (OSError, UnicodeDecodeError) as exc:
            result["errors"].append(
                f"{audit_path.relative_to(root).as_posix()}: {type(exc).__name__}"
            )
            continue
        for key in (
            "folded_days",
            "source_entries",
            "rolled_up_entries",
            "removed_entries",
            "added_fold_records",
            "updated_fold_records",
        ):
            result[key] += int(folded[key])
        if folded["touched"]:
            result["files_touched"].append(str(folded["touched"]))


def fold_old_entries(root: Path, *, days: int = 30, dry_run: bool = False) -> dict[str, Any]:
    """Create idempotent sidecar rollups without modifying raw audit JSONL."""
    result: dict[str, Any] = {
        "ok": False,
        "folded_days": 0,
        "source_entries": 0,
        "rolled_up_entries": 0,
        "removed_entries": 0,
        "added_fold_records": 0,
        "updated_fold_records": 0,
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
            _fold_files_locked(root, cutoff=cutoff, dry_run=dry_run, result=result)
    except OSError as exc:
        result["errors"].append(f"audit transaction: {type(exc).__name__}")
    result["ok"] = not bool(result["errors"])
    return result
