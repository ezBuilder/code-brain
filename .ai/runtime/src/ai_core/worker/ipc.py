from __future__ import annotations

import hashlib
import json
import secrets
from pathlib import Path
from typing import Any

from ai_core import __version__
from ai_core.policy import is_ci

PROTOCOL_VERSION = 1
CI_READONLY_TOKEN = "__ci_readonly_no_worker_token__"
REQUIRED_ENVELOPE = {
    "protocol_version",
    "token",
    "root_id",
    "root_hash",
    "machine_id_hash",
    "request_id",
}


class IpcError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def token_path(root: Path) -> Path:
    return root / ".ai" / "cache" / "run" / "worker.token"


def get_or_create_token(root: Path) -> str:
    path = token_path(root)
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    if is_ci():
        return CI_READONLY_TOKEN
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(32)
    path.write_text(token + "\n", encoding="utf-8")
    path.chmod(0o600)
    return token


def machine_id_hash(root: Path) -> str:
    trust_root = root / ".ai" / "trust" / "machines"
    material = []
    for path in sorted(trust_root.glob("*.pub.toml")):
        material.append(path.read_text(encoding="utf-8"))
    return hashlib.sha256("\n".join(material).encode("utf-8")).hexdigest()


def root_hash(root: Path) -> str:
    return hashlib.sha256(root.resolve().as_posix().encode("utf-8")).hexdigest()


def build_envelope(root: Path, *, request_id: str = "health") -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "token": get_or_create_token(root),
        "root_id": root.name,
        "root_hash": root_hash(root),
        "machine_id_hash": machine_id_hash(root),
        "request_id": request_id,
    }


def validate_envelope(root: Path, envelope: dict[str, Any]) -> None:
    if "protocol_version" not in envelope:
        raise IpcError("INCOMPATIBLE_VERSION", "missing protocol version")
    if envelope["protocol_version"] != PROTOCOL_VERSION:
        raise IpcError("INCOMPATIBLE_VERSION", "protocol major mismatch")
    if "token" not in envelope:
        raise IpcError("UNAUTHORIZED", "missing worker token")
    if "root_hash" not in envelope:
        raise IpcError("UNAUTHORIZED", "missing root hash")
    missing = sorted(REQUIRED_ENVELOPE - set(envelope))
    if missing:
        raise IpcError("INVALID_REQUEST", "missing envelope fields: " + ", ".join(missing))
    if envelope["token"] != get_or_create_token(root):
        raise IpcError("UNAUTHORIZED", "worker token mismatch")
    if envelope["root_hash"] != root_hash(root):
        raise IpcError("UNAUTHORIZED", "root hash mismatch")


def health(root: Path, envelope: dict[str, Any] | None = None) -> dict[str, Any]:
    effective = envelope or build_envelope(root)
    validate_envelope(root, effective)
    return {
        "ok": True,
        "protocol_version": PROTOCOL_VERSION,
        "runtime_version": __version__,
        "methods": ["health", "context_pack", "policy_check", "enqueue_event", "request_rebuild", "flush", "shutdown"],
    }


def parse_envelope(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    return json.loads(raw)
