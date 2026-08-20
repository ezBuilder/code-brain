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
AUTONOMOUS_ROUND_PREFIX = "autonomous-round-"
AUTONOMOUS_ROUND_MAX_BYTES = 256_000
AUTONOMOUS_ROUND_MAX_FILES = 200
_ROUND_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")

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


def _round_required_text(
    container: dict[str, Any],
    key: str,
    *,
    field: str,
    issues: list[str],
) -> str:
    value = container.get(key)
    if not isinstance(value, str) or not value.strip():
        issues.append(f"{field}: required_text")
        return ""
    return value.strip()


def _round_required_mapping(
    container: dict[str, Any],
    key: str,
    *,
    field: str,
    issues: list[str],
) -> dict[str, Any]:
    value = container.get(key)
    if not isinstance(value, dict):
        issues.append(f"{field}: required_object")
        return {}
    return value


def _round_path_list(
    value: Any,
    *,
    field: str,
    issues: list[str],
    require_nonempty: bool = False,
) -> list[str]:
    if not isinstance(value, list):
        issues.append(f"{field}: required_list")
        return []
    if require_nonempty and not value:
        issues.append(f"{field}: empty")
        return []
    paths: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            issues.append(f"{field}[{index}]: invalid_path")
            continue
        parsed, symbol = _split_search_path(item)
        if parsed is None or symbol is not None:
            issues.append(f"{field}[{index}]: invalid_path")
            continue
        paths.append(parsed)
    return paths


def validate_autonomous_round_record(record: Any) -> dict[str, Any]:
    """Validate one typed autonomous-round report without mutating any ledger.

    Error details intentionally contain schema coordinates only. Untrusted report
    values are never reflected into doctor output, which keeps this validation
    safe for reports assembled from research and tool observations.
    """
    issues: list[str] = []
    if not isinstance(record, dict):
        return {"ok": False, "round_id": "", "issues": ["record: required_object"]}

    round_id = _round_required_text(record, "round_id", field="round_id", issues=issues)
    if round_id and _ROUND_ID_RE.fullmatch(round_id) is None:
        issues.append("round_id: invalid_format")

    start = _round_required_mapping(record, "start", field="start", issues=issues)
    if start:
        sha = _round_required_text(start, "sha", field="start.sha", issues=issues)
        if sha and _GIT_SHA_RE.fullmatch(sha) is None:
            issues.append("start.sha: invalid_git_sha")
        _round_required_text(start, "branch", field="start.branch", issues=issues)
        _round_path_list(start.get("dirty_paths"), field="start.dirty_paths", issues=issues)

    research = _round_required_mapping(record, "research", field="research", issues=issues)
    if research:
        _round_required_text(research, "question", field="research.question", issues=issues)
        sources = research.get("sources")
        if not isinstance(sources, list) or not sources:
            issues.append("research.sources: required_nonempty_list")
        else:
            for index, source in enumerate(sources):
                prefix = f"research.sources[{index}]"
                if not isinstance(source, dict):
                    issues.append(f"{prefix}: required_object")
                    continue
                _round_required_text(source, "source", field=f"{prefix}.source", issues=issues)
                _round_required_text(source, "freshness", field=f"{prefix}.freshness", issues=issues)
                _round_required_text(source, "local_repro", field=f"{prefix}.local_repro", issues=issues)

    task = _round_required_mapping(record, "task", field="task", issues=issues)
    if task:
        _round_required_text(task, "task_id", field="task.task_id", issues=issues)
        _round_path_list(
            task.get("owned_paths"),
            field="task.owned_paths",
            issues=issues,
            require_nonempty=True,
        )
        _round_path_list(task.get("protected_paths"), field="task.protected_paths", issues=issues)
        _round_path_list(task.get("changed_paths"), field="task.changed_paths", issues=issues)
        acceptance = task.get("acceptance")
        if not isinstance(acceptance, list) or not acceptance:
            issues.append("task.acceptance: required_nonempty_list")
        else:
            for index, item in enumerate(acceptance):
                prefix = f"task.acceptance[{index}]"
                if not isinstance(item, dict):
                    issues.append(f"{prefix}: required_object")
                    continue
                _round_required_text(item, "command", field=f"{prefix}.command", issues=issues)
                _round_required_text(item, "observed", field=f"{prefix}.observed", issues=issues)
                artifact_path = _round_required_text(
                    item,
                    "artifact_path",
                    field=f"{prefix}.artifact_path",
                    issues=issues,
                )
                if artifact_path:
                    parsed, symbol = _split_search_path(artifact_path)
                    if parsed is None or symbol is not None:
                        issues.append(f"{prefix}.artifact_path: invalid_path")
                exit_code = item.get("exit_code")
                if not isinstance(exit_code, int) or isinstance(exit_code, bool):
                    issues.append(f"{prefix}.exit_code: required_integer")

    reviewer = _round_required_mapping(record, "reviewer", field="reviewer", issues=issues)
    if reviewer:
        _round_required_text(reviewer, "verdict", field="reviewer.verdict", issues=issues)
        evidence = reviewer.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            issues.append("reviewer.evidence: required_nonempty_list")
        else:
            for index, item in enumerate(evidence):
                prefix = f"reviewer.evidence[{index}]"
                if not isinstance(item, dict):
                    issues.append(f"{prefix}: required_object")
                    continue
                _round_required_text(item, "type", field=f"{prefix}.type", issues=issues)
                _round_required_text(item, "ref", field=f"{prefix}.ref", issues=issues)

    end = _round_required_mapping(record, "end", field="end", issues=issues)
    if end:
        _round_required_text(end, "status", field="end.status", issues=issues)
        _round_required_text(end, "next_trigger", field="end.next_trigger", issues=issues)

    return {
        "ok": not issues,
        "round_id": round_id if round_id and _ROUND_ID_RE.fullmatch(round_id) else "",
        "issues": issues,
    }


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
