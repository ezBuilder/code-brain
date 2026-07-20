from __future__ import annotations

import hashlib
import re
import secrets
from pathlib import Path
from typing import Any

from .memory import append_audit
from .private_write import (
    atomic_write_private_text,
    list_root_confined_directory,
    private_file_lock,
    read_root_confined_bytes,
    read_root_confined_text,
)


PRIVATE_KEY_MAX_BYTES = 256
MACHINE_FILE_MAX_BYTES = 64 * 1024
MACHINE_FILE_MAX_COUNT = 256
MACHINE_NAME_MAX_CHARS = 120
MACHINE_TOML_MAX_FIELDS = 16
_PRIVATE_KEY_RE = re.compile(r"^AGE-SECRET-KEY-[0-9a-f]{64}$")
_PUBLIC_KEY_RE = re.compile(r"^age1[0-9a-f]{64}$")
_MACHINE_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_MACHINE_FILE_RE = re.compile(r"^([0-9a-f]{64})\.pub\.toml$")
_TOML_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")


def private_key_path(root: Path) -> Path:
    return root / ".ai" / "cache" / "identity" / "age.key"


def public_key_from_private(private_key: str) -> str:
    return "age1" + hashlib.sha256(private_key.encode("utf-8")).hexdigest()


def machine_id_hash(public_key: str) -> str:
    return hashlib.sha256(public_key.strip().encode("utf-8")).hexdigest()


def _machine_directory(root: Path) -> Path:
    return Path(root) / ".ai" / "trust" / "machines"


def _machine_path(root: Path, machine_hash: str) -> Path:
    if not isinstance(machine_hash, str) or not _MACHINE_HASH_RE.fullmatch(machine_hash):
        raise ValueError("invalid machine id hash")
    return _machine_directory(root) / f"{machine_hash}.pub.toml"


def _read_private_key(root: Path) -> str | None:
    try:
        text, _state = read_root_confined_text(
            private_key_path(root),
            root=root,
            max_bytes=PRIVATE_KEY_MAX_BYTES,
            require_private=True,
            require_owner=True,
            reject_group_other_writable=True,
        )
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return None
    value = text.strip()
    return value if _PRIVATE_KEY_RE.fullmatch(value) else None


def _get_or_create_private_key(root: Path) -> tuple[str, bool]:
    root = Path(root)
    path = private_key_path(root)
    lock_path = path.with_name(f".{path.name}.lock")
    with private_file_lock(lock_path, root=root):
        existing = _read_private_key(root)
        if existing is not None:
            return existing, False
        private_key = "AGE-SECRET-KEY-" + secrets.token_hex(32)
        atomic_write_private_text(path, private_key + "\n", root=root)
        return private_key, True


def _validate_machine_name(name: object) -> str:
    value = str(name or "").strip()
    if not value or len(value) > MACHINE_NAME_MAX_CHARS or "\x00" in value:
        raise ValueError("invalid machine name")
    if any(ord(char) < 0x20 and char not in {"\t"} for char in value):
        raise ValueError("invalid machine name")
    return value


def _render_machine(data: dict[str, str]) -> str:
    keys = ("name", "public_key", "machine_id_hash", "status")
    return "\n".join(
        f'{key} = "{escape_toml(str(data[key]))}"' for key in keys
    ) + "\n"


def init_machine(root: Path, *, name: str) -> dict[str, Any]:
    root = Path(root)
    clean_name = _validate_machine_name(name)
    private_key, created_key = _get_or_create_private_key(root)
    public_key = public_key_from_private(private_key)
    machine_hash = machine_id_hash(public_key)
    trust_file = _machine_path(root, machine_hash)
    atomic_write_private_text(
        trust_file,
        _render_machine({
            "name": clean_name,
            "public_key": public_key,
            "machine_id_hash": machine_hash,
            "status": "trusted",
        }),
        root=root,
    )
    append_audit(root, action="trust.init", category="trust", payload={"machine_id_hash": machine_hash, "created_key": created_key})
    return {
        "ok": True,
        "machine_id_hash": machine_hash,
        "public_key": public_key,
        "private_key_path": private_key_path(root).relative_to(root).as_posix(),
        "trust_file": trust_file.relative_to(root).as_posix(),
        "trust_machines_hash": trust_machines_hash(root),
    }


def _machine_file_names(root: Path) -> tuple[list[str], list[str]]:
    directory = _machine_directory(root)
    try:
        names = list_root_confined_directory(
            directory,
            root=root,
            max_entries=MACHINE_FILE_MAX_COUNT,
        )
    except FileNotFoundError:
        return [], []
    except OSError:
        return [], [".ai/trust/machines:untrusted"]
    return [name for name in names if _MACHINE_FILE_RE.fullmatch(name)], []


def _read_machine_file(root: Path, name: str) -> tuple[dict[str, str] | None, bytes | None]:
    path = _machine_directory(root) / name
    try:
        raw, _state = read_root_confined_bytes(
            path,
            root=root,
            max_bytes=MACHINE_FILE_MAX_BYTES,
            require_private=False,
            require_owner=True,
            reject_group_other_writable=True,
        )
        data = parse_simple_toml(raw.decode("utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, ValueError):
        return None, None
    return data, raw


def _machine_record_valid(name: str, data: dict[str, str]) -> bool:
    match = _MACHINE_FILE_RE.fullmatch(name)
    if match is None:
        return False
    expected_filename_hash = match.group(1)
    public_key = data.get("public_key", "")
    machine_hash = data.get("machine_id_hash", "")
    machine_name = data.get("name", "")
    return bool(
        _PUBLIC_KEY_RE.fullmatch(public_key)
        and _MACHINE_HASH_RE.fullmatch(machine_hash)
        and machine_hash == expected_filename_hash
        and machine_hash == machine_id_hash(public_key)
        and data.get("status") in {"trusted", "revoked"}
        and machine_name
        and len(machine_name) <= MACHINE_NAME_MAX_CHARS
        and "\x00" not in machine_name
    )


def inspect_machine_files(root: Path) -> tuple[list[dict[str, str]], list[str]]:
    root = Path(root)
    names, invalid = _machine_file_names(root)
    machines: list[dict[str, str]] = []
    for name in names:
        path = _machine_directory(root) / name
        data, _raw = _read_machine_file(root, name)
        rel = path.relative_to(root).as_posix()
        if data is None or not _machine_record_valid(name, data):
            invalid.append(rel)
            continue
        machines.append({"path": rel, **data})
    return machines, sorted(set(invalid))


def trust_machines_hash(root: Path) -> str:
    root = Path(root)
    names, invalid = _machine_file_names(root)
    parts: list[str] = []
    for name in names:
        _data, raw = _read_machine_file(root, name)
        if raw is None:
            parts.append(f"{name}:untrusted")
        else:
            parts.append(f"{name}:{hashlib.sha256(raw).hexdigest()}")
    parts.extend(f"!{value}" for value in invalid)
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def list_machines(root: Path) -> dict[str, Any]:
    machines, invalid = inspect_machine_files(root)
    payload: dict[str, Any] = {
        "ok": not invalid,
        "machines": machines,
        "trust_machines_hash": trust_machines_hash(root),
    }
    if invalid:
        payload["invalid"] = invalid
    return payload


def revoke_machine(root: Path, machine_hash: str) -> dict[str, Any]:
    root = Path(root)
    path = _machine_path(root, machine_hash)
    data, _raw = _read_machine_file(root, path.name)
    if data is None:
        raise FileNotFoundError("machine not found")
    if not _machine_record_valid(path.name, data):
        raise ValueError("machine trust record is invalid")
    data["status"] = "revoked"
    atomic_write_private_text(path, _render_machine(data), root=root)
    append_audit(root, action="trust.revoke", category="trust", payload={"machine_id_hash": machine_hash})
    return {
        "ok": True,
        "machine_id_hash": machine_hash,
        "status": "revoked",
        "trust_machines_hash": trust_machines_hash(root),
    }


def parse_simple_toml(text: str) -> dict[str, str]:
    if not isinstance(text, str) or len(text.encode("utf-8")) > MACHINE_FILE_MAX_BYTES:
        raise ValueError("invalid machine trust record")
    result: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if (
            not separator
            or not _TOML_KEY_RE.fullmatch(key)
            or key in result
            or len(result) >= MACHINE_TOML_MAX_FIELDS
            or len(value) < 2
            or value[0] != '"'
            or value[-1] != '"'
        ):
            raise ValueError(f"invalid machine trust syntax at line {line_number}")
        result[key] = _unescape_toml(value[1:-1])
    return result


def _unescape_toml(value: str) -> str:
    output: list[str] = []
    escaped = False
    for char in value:
        if escaped:
            if char not in {'"', "\\"}:
                raise ValueError("invalid machine trust escape")
            output.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        else:
            output.append(char)
    if escaped:
        raise ValueError("invalid machine trust escape")
    return "".join(output)


def escape_toml(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')

