import hashlib
import re
from pathlib import Path
from typing import Any

from .memory import append_audit, append_jsonl, now_iso, read_jsonl_all
from .redact import redact_value

STATUSES = {"open", "verified_fixed", "accepted_risk", "false_positive"}
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


def ledger_path(root: Path) -> Path:
    return root / ".ai" / "memory" / "security-findings.jsonl"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _clean(value: str, limit: int) -> str:
    return str(redact_value(str(value))).strip()[:limit]


def _finding_id(*, affected_path: str, finding_type: str, evidence_hash: str) -> str:
    key = "\0".join((affected_path, finding_type, evidence_hash))
    return f"sec-{_hash(key)[:16]}"


def record(
    root: Path,
    *,
    affected_path: str,
    finding_type: str,
    repro_command: str,
    verification_command: str,
    detail_summary: str,
    evidence_hash: str = "",
    status: str = "open",
    source: str = "agent",
) -> dict[str, Any]:
    if status not in STATUSES:
        raise ValueError(f"invalid security finding status: {status}")
    affected_path = str(affected_path).strip()
    if (
        not affected_path
        or affected_path.startswith(("/", "~"))
        or "\\" in affected_path
        or _WINDOWS_DRIVE.match(affected_path)
        or ".." in Path(affected_path).parts
    ):
        raise ValueError("affected_path must be repository-relative")
    affected_path = _clean(affected_path, 500)
    finding_type = _clean(finding_type, 120)
    detail_summary = _clean(detail_summary, 1000)
    repro_command = _clean(repro_command, 1000)
    verification_command = _clean(verification_command, 1000)
    evidence_hash = _clean(evidence_hash, 128).lower() or _hash(detail_summary)
    if not all((affected_path, finding_type, detail_summary, repro_command, verification_command, evidence_hash)):
        raise ValueError("affected_path, finding_type, detail_summary, repro_command, and verification_command are required")
    rec = {
        "schema_version": 1,
        "id": _finding_id(affected_path=affected_path, finding_type=finding_type, evidence_hash=evidence_hash),
        "recorded_at": now_iso(),
        "affected_path": affected_path,
        "finding_type": finding_type,
        "status": status,
        "evidence_hash": evidence_hash,
        "detail_summary": detail_summary,
        "repro_command": repro_command,
        "verification_command": verification_command,
        "source": str(source or "agent")[:80],
    }
    append_jsonl(ledger_path(root), rec)
    append_audit(root, action="security_finding.record", category="security", payload={"id": rec["id"], "status": status})
    return {"ok": True, "record": rec}


def update(
    root: Path,
    *,
    finding_id: str,
    status: str,
    verification_command: str,
    source: str = "agent",
) -> dict[str, Any]:
    if status not in STATUSES:
        raise ValueError(f"invalid security finding status: {status}")
    current = _latest(root).get(finding_id)
    if current is None:
        raise ValueError(f"security finding not found: {finding_id}")
    return record(
        root,
        affected_path=str(current.get("affected_path") or ""),
        finding_type=str(current.get("finding_type") or ""),
        detail_summary=str(current.get("detail_summary") or ""),
        evidence_hash=str(current.get("evidence_hash") or ""),
        repro_command=str(current.get("repro_command") or ""),
        verification_command=verification_command,
        status=status,
        source=source,
    )


def list_records(root: Path, *, status: str | None = None, limit: int = 50) -> dict[str, Any]:
    if status and status not in STATUSES:
        raise ValueError(f"invalid security finding status: {status}")
    limit = max(1, min(int(limit), 200))
    records = list(_latest(root).values())
    if status:
        records = [rec for rec in records if rec.get("status") == status]
    records.sort(key=lambda rec: str(rec.get("updated_at") or rec.get("recorded_at") or ""), reverse=True)
    items = records[:limit]
    return {"ok": True, "count": len(items), "records": items}


def _latest(root: Path) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for rec in read_jsonl_all(ledger_path(root)):
        if rec.get("id"):
            latest[str(rec["id"])] = rec
    return latest
