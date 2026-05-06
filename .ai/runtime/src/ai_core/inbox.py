from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .memory import append_audit
from .redact import redact_value

APPROVAL_GATES = {"revoke_all", "secret_class_change", "model_change", "remote_enable", "bulk_index_drop"}


def inbox_dir(root: Path) -> Path:
    path = root / ".ai" / "memory" / "inbox"
    path.mkdir(parents=True, exist_ok=True)
    return path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def request_approval(root: Path, gate: str, summary: str, payload: dict[str, Any], *, ttl_hours: int = 24) -> dict[str, Any]:
    if gate not in APPROVAL_GATES:
        raise ValueError(f"approval gate must be one of: {', '.join(sorted(APPROVAL_GATES))}")
    approval_id = secrets.token_hex(16)
    approval_code = secrets.token_hex(4)
    record = {
        "approval_id": approval_id,
        "approval_id_code": approval_code,
        "gate": gate,
        "summary": summary,
        "payload": redact_value(payload),
        "status": "pending",
        "created_at": now_iso(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).isoformat().replace("+00:00", "Z"),
        "instructions": f"Run ai inbox approve {approval_id} or ai inbox reject {approval_id}.",
    }
    path = inbox_dir(root) / f"{approval_id}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    append_audit(root, action="inbox.request", category="approval", payload={"approval_id": approval_id, "gate": gate})
    return {"ok": True, "approval": public_record(record)}


def list_approvals(root: Path) -> dict[str, Any]:
    records = [public_record(read_record(path)) for path in sorted(inbox_dir(root).glob("*.json"))]
    return {"ok": True, "approvals": records}


def decide(root: Path, approval_id: str, status: str) -> dict[str, Any]:
    if status not in {"approved", "rejected"}:
        raise ValueError("status must be approved or rejected")
    path = inbox_dir(root) / f"{approval_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"approval not found: {approval_id}")
    record = read_record(path)
    if record.get("status") != "pending":
        raise ValueError("approval is not pending")
    record["status"] = status
    record["decided_at"] = now_iso()
    path.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    append_audit(root, action=f"inbox.{status}", category="approval", payload={"approval_id": approval_id, "gate": record.get("gate")})
    return {"ok": True, "approval": public_record(record)}


def read_record(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def public_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "approval_id": record["approval_id"],
        "approval_id_code": record["approval_id_code"],
        "gate": record["gate"],
        "summary": record["summary"],
        "status": record["status"],
        "expires_at": record["expires_at"],
        "instructions": record["instructions"],
    }

