from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from .memory import append_audit, append_jsonl, now_iso, read_jsonl_all, rotate_jsonl_tail
from .redact import redact_value

STATUSES = ("candidate", "curated", "verified", "rejected")
MAX_QUERY_CHARS = 512
MAX_SNIPPET_CHARS = 1200
MAX_NOTE_CHARS = 512
MAX_SYMBOL_CHARS = 240
EVIDENCE_MAX_BYTES = 4_000_000
EVIDENCE_KEEP = 5000

_DENIED_NAMES = {
    ".env",
    "auth.json",
    "credentials.json",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
}
_DENIED_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
_DENIED_PARTS = {"secret", "secrets", ".secrets"}
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


def evidence_path(root: Path) -> Path:
    return root / ".ai" / "memory" / "evidence.jsonl"


def rotate_ledger(root: Path, *, dry_run: bool = False) -> dict[str, Any]:
    return rotate_jsonl_tail(
        evidence_path(root),
        max_bytes=EVIDENCE_MAX_BYTES,
        keep_lines=EVIDENCE_KEEP,
        dry_run=dry_run,
    )


def _clean_text(value: Any, *, max_chars: int) -> str:
    return str(redact_value("" if value is None else value)).strip()[:max_chars]


def _split_search_path(raw_value: Any) -> tuple[str | None, str | None]:
    raw = str(raw_value or "").strip().replace("\\", "/")
    if not raw or "\x00" in raw or raw.startswith(("~", "/")) or _WINDOWS_DRIVE.match(raw):
        return None, None

    path_part = raw
    symbol: str | None = None
    if ":" in raw:
        head, tail = raw.split(":", 1)
        if tail and ("/" in tail or "\\" in tail):
            return None, None
        path_part = head
        symbol = _clean_text(tail, max_chars=MAX_SYMBOL_CHARS) if tail else None

    rel = PurePosixPath(path_part)
    if rel.is_absolute() or not rel.parts or any(part in {"", ".", ".."} for part in rel.parts):
        return None, None

    lowered = [part.lower() for part in rel.parts]
    if any(part in _DENIED_PARTS for part in lowered):
        return None, None
    if lowered[-1] in _DENIED_NAMES or lowered[-1].endswith(_DENIED_SUFFIXES):
        return None, None
    return rel.as_posix(), symbol or None


def evidence_id(*, source: str, query: str, path: str, snippet: str, symbol: str | None = None) -> str:
    payload = {
        "path": path,
        "query": query,
        "snippet_sha256": hashlib.sha256(snippet.encode("utf-8")).hexdigest(),
        "symbol": symbol or "",
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "evid-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _candidate_record(
    *,
    query: str,
    result: dict[str, Any],
    source: str,
    rank: int,
    observed_at: str,
) -> dict[str, Any] | None:
    path, symbol = _split_search_path(result.get("path"))
    if path is None:
        return None
    query_clean = _clean_text(query, max_chars=MAX_QUERY_CHARS)
    snippet = _clean_text(result.get("snippet", ""), max_chars=MAX_SNIPPET_CHARS)
    source_clean = _clean_text(source or "search", max_chars=64) or "search"
    record = {
        "id": evidence_id(source=source_clean, query=query_clean, path=path, snippet=snippet, symbol=symbol),
        "status": "candidate",
        "source": source_clean,
        "query": query_clean,
        "path": path,
        "rank": int(rank),
        "snippet": snippet,
        "provenance": redact_value(result.get("provenance") or {}),
        "observed_at": observed_at,
    }
    if symbol:
        record["symbol"] = symbol
    return record


def append_candidate_results(
    root: Path,
    *,
    query: str,
    results: list[dict[str, Any]],
    source: str = "search",
) -> dict[str, Any]:
    path = evidence_path(root)
    existing_ids = {str(entry.get("id")) for entry in read_jsonl_all(path)}
    observed_at = now_iso()
    appended: list[str] = []
    skipped = 0
    for index, result in enumerate(results):
        if not isinstance(result, dict):
            skipped += 1
            continue
        record = _candidate_record(query=query, result=result, source=source, rank=index + 1, observed_at=observed_at)
        if record is None:
            skipped += 1
            continue
        if record["id"] in existing_ids:
            skipped += 1
            continue
        append_jsonl(path, record)
        existing_ids.add(record["id"])
        appended.append(record["id"])
    if appended:
        rotate_ledger(root)
    if appended:
        append_audit(
            root,
            action="evidence.candidates",
            category="evidence",
            payload={"source": source, "query": _clean_text(query, max_chars=MAX_QUERY_CHARS), "appended": len(appended)},
        )
    return {
        "ok": True,
        "path": path.relative_to(root).as_posix(),
        "appended": len(appended),
        "skipped": skipped,
        "ids": appended,
    }


def record_evidence(
    root: Path,
    *,
    query: str,
    path: str,
    status: str = "candidate",
    snippet: str = "",
    source: str = "agent",
    note: str = "",
) -> dict[str, Any]:
    if status not in STATUSES:
        return {"ok": False, "reason": "invalid_status", "statuses": list(STATUSES)}
    parsed_path, symbol = _split_search_path(path)
    if parsed_path is None:
        return {"ok": False, "reason": "invalid_path"}
    query_clean = _clean_text(query, max_chars=MAX_QUERY_CHARS)
    if not query_clean:
        return {"ok": False, "reason": "empty_query"}
    snippet_clean = _clean_text(snippet, max_chars=MAX_SNIPPET_CHARS)
    source_clean = _clean_text(source or "agent", max_chars=64) or "agent"
    record = {
        "id": evidence_id(source=source_clean, query=query_clean, path=parsed_path, snippet=snippet_clean, symbol=symbol),
        "status": status,
        "source": source_clean,
        "query": query_clean,
        "path": parsed_path,
        "snippet": snippet_clean,
        "recorded_at": now_iso(),
    }
    if symbol:
        record["symbol"] = symbol
    note_clean = _clean_text(note, max_chars=MAX_NOTE_CHARS)
    if note_clean:
        record["note"] = note_clean
    current = _latest_record(root, record["id"])
    if current is not None:
        return {"ok": True, "changed": False, "record": current}
    append_jsonl(evidence_path(root), record)
    rotate_ledger(root)
    append_audit(root, action="evidence.record", category="evidence", payload={"id": record["id"], "status": status})
    return {"ok": True, "changed": True, "record": record}


def list_evidence(
    root: Path,
    *,
    status: str | None = None,
    query: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    if status is not None and status not in STATUSES:
        return {"ok": False, "reason": "invalid_status", "statuses": list(STATUSES)}
    needle = str(query or "").strip().casefold()
    records = read_jsonl_all(evidence_path(root))
    latest: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in reversed(records):
        eid = str(entry.get("id") or "")
        if not eid or eid in seen:
            continue
        seen.add(eid)
        if status is not None and entry.get("status") != status:
            continue
        if needle and needle not in str(entry.get("query") or "").casefold():
            continue
        latest.append(entry)
        if len(latest) >= max(0, limit):
            break
    return {"ok": True, "evidence": latest, "records": latest, "count": len(latest)}


def _latest_record(root: Path, evidence_id_value: str) -> dict[str, Any] | None:
    for entry in reversed(read_jsonl_all(evidence_path(root))):
        if str(entry.get("id") or "") == evidence_id_value:
            return entry
    return None


def _transition_allowed(current: str, target: str) -> bool:
    allowed = {
        "candidate": {"curated", "verified", "rejected"},
        "curated": {"verified", "rejected"},
        "verified": {"rejected"},
        "rejected": set(),
    }
    return target in allowed.get(current, set())


def set_evidence_status(
    root: Path,
    *,
    evidence_id_value: str,
    status: str,
    note: str = "",
    source: str = "operator",
) -> dict[str, Any]:
    eid = str(evidence_id_value or "").strip()
    if not eid:
        return {"ok": False, "reason": "empty_id"}
    if status not in STATUSES:
        return {"ok": False, "reason": "invalid_status", "statuses": list(STATUSES)}
    current = _latest_record(root, eid)
    if current is None:
        return {"ok": False, "reason": "not_found", "id": eid}
    current_status = str(current.get("status") or "candidate")
    if current_status == status:
        return {"ok": True, "changed": False, "record": current}
    if not _transition_allowed(current_status, status):
        return {
            "ok": False,
            "reason": "invalid_transition",
            "id": eid,
            "current_status": current_status,
            "target_status": status,
        }

    update = dict(current)
    update["status"] = status
    update["updated_at"] = now_iso()
    update["status_source"] = _clean_text(source or "operator", max_chars=64) or "operator"
    note_clean = _clean_text(note, max_chars=MAX_NOTE_CHARS)
    if note_clean:
        update["note"] = note_clean
    append_jsonl(evidence_path(root), update)
    rotate_ledger(root)
    append_audit(root, action="evidence.set_status", category="evidence", payload={"id": eid, "status": status})
    return {"ok": True, "changed": True, "record": update}
