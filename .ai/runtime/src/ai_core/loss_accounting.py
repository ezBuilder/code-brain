from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .private_write import atomic_write_private_text, private_file_lock, read_root_confined_text
from .redact import redact_value

SCHEMA_VERSION = 1
MAX_BYTES = 64_000
MAX_DOMAINS = 32
MAX_REASONS = 32
MAX_EXAMPLES = 10
SNAPSHOT_LAST_ERRORS = 3
SNAPSHOT_LAST_EXAMPLES = 3
SNAPSHOT_TEXT_BYTES = 120
_DOMAIN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def accounting_path(root: Path) -> Path:
    return Path(root) / ".ai" / "cache" / "loss-accounting.json"


def _lock_path(root: Path) -> Path:
    return Path(root) / ".ai" / "cache" / ".loss-accounting.lock"


def _count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _bounded_examples(values: Iterable[object]) -> list[str]:
    examples: list[str] = []
    for value in values:
        rendered = str(redact_value(value))[:240]
        if rendered and rendered not in examples:
            examples.append(rendered)
        if len(examples) >= MAX_EXAMPLES:
            break
    return examples


def _count_and_examples(values: Iterable[object]) -> tuple[int, list[str]]:
    count = 0
    examples: list[str] = []
    for value in values:
        count += 1
        if len(examples) >= MAX_EXAMPLES:
            continue
        rendered = str(redact_value(value))[:240]
        if rendered and rendered not in examples:
            examples.append(rendered)
    return count, examples


def loss_event(
    *,
    domain: str,
    operation: str,
    applied: bool,
    dry_run: bool = False,
    files_before: int = 0,
    files_after: int = 0,
    bytes_before: int = 0,
    bytes_after: int = 0,
    records_before: int = 0,
    records_after: int = 0,
    reasons: Mapping[str, object] | None = None,
    errors: Iterable[object] = (),
    examples: Iterable[object] = (),
) -> dict[str, Any]:
    if not _DOMAIN.fullmatch(str(domain)):
        raise ValueError("invalid loss-accounting domain")
    before_files = _count(files_before)
    after_files = _count(files_after)
    before_bytes = _count(bytes_before)
    after_bytes = _count(bytes_after)
    before_records = _count(records_before)
    after_records = _count(records_after)
    reason_counts = {
        str(name)[:64]: _count(value)
        for name, value in sorted((reasons or {}).items())
        if isinstance(name, str) and _count(value) > 0
    }
    if len(reason_counts) > MAX_REASONS:
        reason_counts = dict(list(reason_counts.items())[:MAX_REASONS])
    error_count, error_examples = _count_and_examples(errors)
    event = {
        "schema_version": SCHEMA_VERSION,
        "domain": domain,
        "operation": str(operation)[:240],
        "at": _now_iso(),
        "applied": bool(applied and not dry_run),
        "dry_run": bool(dry_run),
        "changed": bool(
            before_files != after_files
            or before_bytes != after_bytes
            or before_records != after_records
        ),
        "files": {
            "before": before_files,
            "after": after_files,
            "removed": max(0, before_files - after_files),
        },
        "bytes": {
            "before": before_bytes,
            "after": after_bytes,
            "removed": max(0, before_bytes - after_bytes),
        },
        "records": {
            "before": before_records,
            "after": after_records,
            "removed": max(0, before_records - after_records),
        },
        "reasons": reason_counts,
        "error_count": error_count,
        "errors": error_examples,
        "examples": _bounded_examples(examples),
    }
    return redact_value(event)


def _empty_snapshot() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": None,
        "totals": {
            "events": 0,
            "applied_events": 0,
            "removed_files": 0,
            "removed_bytes": 0,
            "removed_records": 0,
            "error_events": 0,
        },
        "domains": {},
    }


def _safe_load(root: Path) -> tuple[dict[str, Any] | None, str | None]:
    path = accounting_path(root)
    try:
        text, _state = read_root_confined_text(
            path,
            root=Path(root),
            max_bytes=MAX_BYTES,
            require_private=True,
        )
    except FileNotFoundError:
        return _empty_snapshot(), None
    except OSError as exc:
        return None, f"unreadable:{exc}"
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None, "invalid_json"
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        return None, "invalid_schema"
    totals = payload.get("totals")
    domains = payload.get("domains")
    if not isinstance(totals, dict) or not isinstance(domains, dict) or len(domains) > MAX_DOMAINS:
        return None, "invalid_shape"
    return payload, None


def _increment(container: dict[str, Any], key: str, amount: int) -> None:
    container[key] = _count(container.get(key)) + _count(amount)


def _snapshot_strings(values: object, *, limit: int) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for value in values[: max(0, int(limit))]:
        encoded = str(redact_value(value)).encode("utf-8")[:SNAPSHOT_TEXT_BYTES]
        result.append(encoded.decode("utf-8", errors="ignore"))
    return result


def _snapshot_event(event: dict[str, Any]) -> dict[str, Any]:
    """Persist a fixed-size last-event summary, never the full transient event."""
    return {
        "schema_version": SCHEMA_VERSION,
        "domain": str(event.get("domain") or "")[:64],
        "operation": str(event.get("operation") or "")[:120],
        "at": str(event.get("at") or "")[:40],
        "applied": event.get("applied") is True,
        "dry_run": event.get("dry_run") is True,
        "changed": event.get("changed") is True,
        "files": event.get("files") if isinstance(event.get("files"), dict) else {},
        "bytes": event.get("bytes") if isinstance(event.get("bytes"), dict) else {},
        "records": event.get("records") if isinstance(event.get("records"), dict) else {},
        "reasons": dict(list((event.get("reasons") or {}).items())[:MAX_REASONS])
        if isinstance(event.get("reasons"), dict)
        else {},
        "error_count": _count(event.get("error_count")),
        "errors": _snapshot_strings(event.get("errors"), limit=SNAPSHOT_LAST_ERRORS),
        "examples": _snapshot_strings(event.get("examples"), limit=SNAPSHOT_LAST_EXAMPLES),
    }


def record_event(root: Path, event: dict[str, Any]) -> dict[str, Any]:
    if event.get("schema_version") != SCHEMA_VERSION:
        return {"ok": False, "recorded": False, "reason": "invalid_event_schema"}
    domain = event.get("domain")
    if not isinstance(domain, str) or not _DOMAIN.fullmatch(domain):
        return {"ok": False, "recorded": False, "reason": "invalid_domain"}
    if event.get("dry_run") is True:
        return {"ok": True, "recorded": False, "reason": "dry_run"}
    applied = event.get("applied") is True
    if not applied and _count(event.get("error_count")) == 0:
        return {"ok": True, "recorded": False, "reason": "not_applied"}
    if event.get("changed") is not True and _count(event.get("error_count")) == 0:
        return {"ok": True, "recorded": False, "reason": "no_change"}

    root = Path(root)
    path = accounting_path(root)
    try:
        with private_file_lock(_lock_path(root), root=root):
            snapshot, error = _safe_load(root)
            if snapshot is None:
                return {"ok": False, "recorded": False, "reason": error}
            domains = snapshot["domains"]
            if domain not in domains and len(domains) >= MAX_DOMAINS:
                return {"ok": False, "recorded": False, "reason": "domain_limit"}
            domain_state = domains.setdefault(
                domain,
                {
                    "events": 0,
                    "applied_events": 0,
                    "removed_files": 0,
                    "removed_bytes": 0,
                    "removed_records": 0,
                    "error_events": 0,
                    "reasons": {},
                    "last_event": None,
                },
            )
            totals = snapshot["totals"]
            files_removed = _count((event.get("files") or {}).get("removed"))
            bytes_removed = _count((event.get("bytes") or {}).get("removed"))
            records_removed = _count((event.get("records") or {}).get("removed"))
            error_event = 1 if _count(event.get("error_count")) else 0
            for container in (totals, domain_state):
                _increment(container, "events", 1)
                _increment(container, "applied_events", 1 if applied else 0)
                _increment(container, "removed_files", files_removed)
                _increment(container, "removed_bytes", bytes_removed)
                _increment(container, "removed_records", records_removed)
                _increment(container, "error_events", error_event)
            reasons = domain_state.setdefault("reasons", {})
            for reason, value in (event.get("reasons") or {}).items():
                if isinstance(reason, str):
                    _increment(reasons, reason[:64], _count(value))
            if len(reasons) > MAX_REASONS:
                domain_state["reasons"] = dict(sorted(reasons.items())[:MAX_REASONS])
            domain_state["last_event"] = _snapshot_event(event)
            snapshot["updated_at"] = _now_iso()
            encoded = json.dumps(redact_value(snapshot), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
            if len(encoded.encode("utf-8")) > MAX_BYTES:
                return {"ok": False, "recorded": False, "reason": "snapshot_too_large"}
            atomic_write_private_text(path, encoded, root=root)
    except (OSError, ValueError) as exc:
        return {"ok": False, "recorded": False, "reason": str(exc)[:200]}
    return {
        "ok": True,
        "recorded": True,
        "reason": None,
        "path": path.relative_to(root).as_posix(),
    }


def finalize_event(root: Path, event: dict[str, Any]) -> dict[str, Any]:
    result = dict(event)
    accounting = record_event(root, event)
    result["accounting"] = accounting
    return redact_value(result)


def summary(root: Path) -> dict[str, Any]:
    snapshot, error = _safe_load(Path(root))
    if snapshot is None:
        return {
            "ok": False,
            "bounded": True,
            "observed": False,
            "reason": error,
            "policy": {"max_bytes": MAX_BYTES, "max_domains": MAX_DOMAINS},
            "totals": _empty_snapshot()["totals"],
            "domains": {},
        }
    observed = _count(snapshot.get("totals", {}).get("events")) > 0
    return redact_value(
        {
            "ok": True,
            "bounded": True,
            "observed": observed,
            "reason": None if observed else "no_loss_events",
            "updated_at": snapshot.get("updated_at"),
            "policy": {"max_bytes": MAX_BYTES, "max_domains": MAX_DOMAINS},
            "totals": snapshot.get("totals", {}),
            "domains": snapshot.get("domains", {}),
        }
    )


__all__ = [
    "MAX_BYTES",
    "SCHEMA_VERSION",
    "accounting_path",
    "finalize_event",
    "loss_event",
    "record_event",
    "summary",
]
