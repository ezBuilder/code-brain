from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any

from ai_core import __version__
from ai_core.policy import is_ci
from ai_core.private_write import (
    atomic_write_private_text,
    list_root_confined_directory,
    private_file_lock,
    read_root_confined_text,
)

PROTOCOL_VERSION = 1
CI_READONLY_TOKEN = "__ci_readonly_no_worker_token__"
TOKEN_BYTES = 32
TOKEN_HEX_CHARS = TOKEN_BYTES * 2
TOKEN_FILE_MAX_BYTES = 256
ENVELOPE_MAX_BYTES = 64 * 1024
ENVELOPE_MAX_FIELDS = 32
REQUEST_ID_MAX_CHARS = 256
ROOT_ID_MAX_CHARS = 256
MACHINE_FILE_MAX_BYTES = 64 * 1024
MACHINE_FILE_MAX_COUNT = 256
_TOKEN_RE = re.compile(rf"^[0-9a-f]{{{TOKEN_HEX_CHARS}}}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MACHINE_FILE_RE = re.compile(r"^[0-9a-f]{64}\.pub\.toml$")
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


def _read_valid_token(root: Path, path: Path) -> str | None:
    try:
        text, _state = read_root_confined_text(
            path,
            root=root,
            max_bytes=TOKEN_FILE_MAX_BYTES,
            require_private=True,
            require_owner=True,
            reject_group_other_writable=True,
        )
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return None
    token = text.strip()
    return token if _TOKEN_RE.fullmatch(token) else None


def get_or_create_token(root: Path) -> str:
    root = Path(root)
    path = token_path(root)
    if is_ci():
        return _read_valid_token(root, path) or CI_READONLY_TOKEN
    lock_path = path.with_name(f".{path.name}.lock")
    with private_file_lock(lock_path, root=root):
        existing = _read_valid_token(root, path)
        if existing is not None:
            return existing
        token = secrets.token_hex(TOKEN_BYTES)
        atomic_write_private_text(path, token + "\n", root=root)
        return token


def machine_id_hash(root: Path) -> str:
    root = Path(root)
    trust_root = root / ".ai" / "trust" / "machines"
    try:
        names = list_root_confined_directory(
            trust_root,
            root=root,
            max_entries=MACHINE_FILE_MAX_COUNT,
        )
    except (FileNotFoundError, OSError):
        names = []
    material: list[str] = []
    for name in names:
        if not _MACHINE_FILE_RE.fullmatch(name):
            continue
        try:
            text, _state = read_root_confined_text(
                trust_root / name,
                root=root,
                max_bytes=MACHINE_FILE_MAX_BYTES,
                require_private=False,
                require_owner=True,
                reject_group_other_writable=True,
            )
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            continue
        material.append(text)
    return hashlib.sha256("\n".join(material).encode("utf-8")).hexdigest()


def root_hash(root: Path) -> str:
    return hashlib.sha256(Path(os.path.abspath(root)).as_posix().encode("utf-8")).hexdigest()


def build_envelope(root: Path, *, request_id: str = "health") -> dict[str, Any]:
    request_id = str(request_id or "")[:REQUEST_ID_MAX_CHARS]
    return {
        "protocol_version": PROTOCOL_VERSION,
        "token": get_or_create_token(root),
        "root_id": root.name,
        "root_hash": root_hash(root),
        "machine_id_hash": machine_id_hash(root),
        "request_id": request_id,
    }


def validate_envelope(root: Path, envelope: dict[str, Any]) -> None:
    if not isinstance(envelope, dict) or len(envelope) > ENVELOPE_MAX_FIELDS:
        raise IpcError("INVALID_REQUEST", "invalid worker envelope")
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
    token = envelope.get("token")
    if not isinstance(token, str) or len(token) > TOKEN_FILE_MAX_BYTES:
        raise IpcError("UNAUTHORIZED", "invalid worker token")
    if not secrets.compare_digest(token, get_or_create_token(root)):
        raise IpcError("UNAUTHORIZED", "worker token mismatch")
    supplied_root_hash = envelope.get("root_hash")
    if not isinstance(supplied_root_hash, str) or not _SHA256_RE.fullmatch(supplied_root_hash):
        raise IpcError("UNAUTHORIZED", "invalid root hash")
    if not secrets.compare_digest(supplied_root_hash, root_hash(root)):
        raise IpcError("UNAUTHORIZED", "root hash mismatch")
    supplied_machine_hash = envelope.get("machine_id_hash")
    if not isinstance(supplied_machine_hash, str) or not _SHA256_RE.fullmatch(supplied_machine_hash):
        raise IpcError("UNAUTHORIZED", "invalid machine hash")
    if not secrets.compare_digest(supplied_machine_hash, machine_id_hash(root)):
        raise IpcError("UNAUTHORIZED", "machine hash mismatch")
    root_id = envelope.get("root_id")
    expected_root_id = Path(root).name
    if not isinstance(root_id, str) or not root_id or len(root_id) > ROOT_ID_MAX_CHARS:
        raise IpcError("UNAUTHORIZED", "invalid root id")
    if not secrets.compare_digest(root_id, expected_root_id):
        raise IpcError("UNAUTHORIZED", "root id mismatch")
    request_id = envelope.get("request_id")
    if not isinstance(request_id, str) or not request_id or len(request_id) > REQUEST_ID_MAX_CHARS:
        raise IpcError("INVALID_REQUEST", "invalid request id")


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
    if not isinstance(raw, str) or "\x00" in raw or len(raw.encode("utf-8")) > ENVELOPE_MAX_BYTES:
        raise IpcError("INVALID_REQUEST", "invalid worker envelope")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise IpcError("INVALID_REQUEST", "invalid worker envelope") from exc
    if not isinstance(payload, dict) or len(payload) > ENVELOPE_MAX_FIELDS:
        raise IpcError("INVALID_REQUEST", "invalid worker envelope")
    return payload
