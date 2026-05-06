from __future__ import annotations

import hashlib
import secrets
from pathlib import Path
from typing import Any

from .memory import append_audit
from .render import hash_tree


def private_key_path(root: Path) -> Path:
    return root / ".ai" / "cache" / "identity" / "age.key"


def public_key_from_private(private_key: str) -> str:
    return "age1" + hashlib.sha256(private_key.encode("utf-8")).hexdigest()


def machine_id_hash(public_key: str) -> str:
    return hashlib.sha256(public_key.strip().encode("utf-8")).hexdigest()


def init_machine(root: Path, *, name: str) -> dict[str, Any]:
    key_path = private_key_path(root)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if key_path.exists():
        private_key = key_path.read_text(encoding="utf-8").strip()
        created_key = False
    else:
        private_key = "AGE-SECRET-KEY-" + secrets.token_hex(32)
        key_path.write_text(private_key + "\n", encoding="utf-8")
        key_path.chmod(0o600)
        created_key = True
    public_key = public_key_from_private(private_key)
    machine_hash = machine_id_hash(public_key)
    trust_file = root / ".ai" / "trust" / "machines" / f"{machine_hash}.pub.toml"
    trust_file.parent.mkdir(parents=True, exist_ok=True)
    trust_file.write_text(
        "\n".join(
            [
                f'name = "{escape_toml(name)}"',
                f'public_key = "{public_key}"',
                f'machine_id_hash = "{machine_hash}"',
                'status = "trusted"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    append_audit(root, action="trust.init", category="trust", payload={"machine_id_hash": machine_hash, "created_key": created_key})
    return {
        "ok": True,
        "machine_id_hash": machine_hash,
        "public_key": public_key,
        "private_key_path": key_path.relative_to(root).as_posix(),
        "trust_file": trust_file.relative_to(root).as_posix(),
        "trust_machines_hash": hash_tree(root / ".ai" / "trust" / "machines"),
    }


def list_machines(root: Path) -> dict[str, Any]:
    machines = []
    for path in sorted((root / ".ai" / "trust" / "machines").glob("*.pub.toml")):
        data = parse_simple_toml(path.read_text(encoding="utf-8"))
        machines.append({"path": path.relative_to(root).as_posix(), **data})
    return {"ok": True, "machines": machines, "trust_machines_hash": hash_tree(root / ".ai" / "trust" / "machines")}


def revoke_machine(root: Path, machine_hash: str) -> dict[str, Any]:
    path = root / ".ai" / "trust" / "machines" / f"{machine_hash}.pub.toml"
    if not path.exists():
        raise FileNotFoundError(f"machine not found: {machine_hash}")
    data = parse_simple_toml(path.read_text(encoding="utf-8"))
    data["status"] = "revoked"
    path.write_text(
        "\n".join(f'{key} = "{escape_toml(str(value))}"' for key, value in data.items()) + "\n",
        encoding="utf-8",
    )
    append_audit(root, action="trust.revoke", category="trust", payload={"machine_id_hash": machine_hash})
    return {"ok": True, "machine_id_hash": machine_hash, "status": "revoked", "trust_machines_hash": hash_tree(root / ".ai" / "trust" / "machines")}


def parse_simple_toml(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip('"')
    return result


def escape_toml(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')

