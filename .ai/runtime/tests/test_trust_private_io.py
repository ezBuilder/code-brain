from __future__ import annotations

import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ai_core import render, trust
from ai_core.doctor import check_trust


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_init_machine_repairs_private_key_symlink_without_external_mutation(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    external = tmp_path / "outside-age.key"
    external.write_text("AGE-SECRET-KEY-" + ("a" * 64) + "\n", encoding="utf-8")
    key = trust.private_key_path(root)
    key.parent.mkdir(parents=True)
    key.symlink_to(external)

    payload = trust.init_machine(root, name="local")

    assert payload["ok"] is True
    assert not key.is_symlink()
    assert key.read_text(encoding="utf-8").strip().startswith("AGE-SECRET-KEY-")
    assert external.read_text(encoding="utf-8") == "AGE-SECRET-KEY-" + ("a" * 64) + "\n"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_init_machine_repairs_private_key_hardlink_without_external_mutation(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    external = tmp_path / "outside-age.key"
    external.write_text("AGE-SECRET-KEY-" + ("b" * 64) + "\n", encoding="utf-8")
    key = trust.private_key_path(root)
    key.parent.mkdir(parents=True)
    os.link(external, key)

    payload = trust.init_machine(root, name="local")

    assert payload["ok"] is True
    assert key.stat().st_ino != external.stat().st_ino
    assert external.read_text(encoding="utf-8") == "AGE-SECRET-KEY-" + ("b" * 64) + "\n"


def test_concurrent_machine_init_uses_single_private_key(tmp_path: Path) -> None:
    root = tmp_path / "repo"

    with ThreadPoolExecutor(max_workers=8) as pool:
        payloads = list(pool.map(lambda _index: trust.init_machine(root, name="local"), range(16)))

    assert len({payload["public_key"] for payload in payloads}) == 1
    assert len({payload["machine_id_hash"] for payload in payloads}) == 1
    key = trust.private_key_path(root)
    assert trust._PRIVATE_KEY_RE.fullmatch(key.read_text(encoding="utf-8").strip())
    listed = trust.list_machines(root)
    assert listed["ok"] is True
    assert len(listed["machines"]) == 1


@pytest.mark.skipif(os.name == "nt", reason="Unix directory symlink semantics")
def test_init_machine_rejects_external_trust_parent_without_mutation(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    external = tmp_path / "outside-machines"
    external.mkdir()
    trust_parent = root / ".ai" / "trust"
    trust_parent.mkdir(parents=True)
    (trust_parent / "machines").symlink_to(external, target_is_directory=True)

    with pytest.raises(OSError):
        trust.init_machine(root, name="local")

    assert list(external.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_init_machine_repairs_linked_trust_file_without_external_mutation(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    private_key = "AGE-SECRET-KEY-" + ("c" * 64)
    key = trust.private_key_path(root)
    key.parent.mkdir(parents=True)
    key.write_text(private_key + "\n", encoding="utf-8")
    key.chmod(0o600)
    public_key = trust.public_key_from_private(private_key)
    machine_hash = trust.machine_id_hash(public_key)
    path = root / ".ai" / "trust" / "machines" / f"{machine_hash}.pub.toml"
    path.parent.mkdir(parents=True)
    external = tmp_path / "outside-machine.toml"
    external.write_text("outside secret\n", encoding="utf-8")
    path.symlink_to(external)

    payload = trust.init_machine(root, name="local")

    assert payload["ok"] is True
    assert not path.is_symlink()
    assert external.read_text(encoding="utf-8") == "outside secret\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_list_and_doctor_flag_linked_machine_file_without_reading_external(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    external = tmp_path / "outside-machine.toml"
    external.write_text("outside secret\n", encoding="utf-8")
    path = root / ".ai" / "trust" / "machines" / (("d" * 64) + ".pub.toml")
    path.parent.mkdir(parents=True)
    path.symlink_to(external)

    listed = trust.list_machines(root)
    check = check_trust(root)

    assert listed["ok"] is False
    assert listed["machines"] == []
    assert path.relative_to(root).as_posix() in listed["invalid"]
    assert check.ok is False
    assert external.read_text(encoding="utf-8") == "outside secret\n"


def test_revoke_rejects_invalid_hash_before_filesystem(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid machine id hash"):
        trust.revoke_machine(tmp_path, "../outside")


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_revoke_rejects_hardlinked_machine_record_without_external_mutation(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    public_key = "age1" + ("e" * 64)
    machine_hash = trust.machine_id_hash(public_key)
    external = tmp_path / "outside-machine.toml"
    data = {
        "name": "outside",
        "public_key": public_key,
        "machine_id_hash": machine_hash,
        "status": "trusted",
    }
    external.write_text(trust._render_machine(data), encoding="utf-8")
    path = root / ".ai" / "trust" / "machines" / f"{machine_hash}.pub.toml"
    path.parent.mkdir(parents=True)
    os.link(external, path)
    original = external.read_text(encoding="utf-8")

    with pytest.raises(FileNotFoundError):
        trust.revoke_machine(root, machine_hash)

    assert external.read_text(encoding="utf-8") == original


def test_trust_hash_matches_safe_flat_tree_contract(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    payload = trust.init_machine(root, name="local")

    assert payload["trust_machines_hash"] == render.hash_tree(root / ".ai" / "trust" / "machines")
    assert render.build_manifest(root)["trust"]["machines_hash"] == payload["trust_machines_hash"]


@pytest.mark.parametrize(
    "text",
    [
        'name = "one"\nname = "two"\n',
        'bad key = "value"\n',
        'name = unquoted\n',
        'name = "bad\\n"\n',
        'name = "unterminated\n',
    ],
)
def test_machine_toml_parser_rejects_invalid_records(text: str) -> None:
    with pytest.raises(ValueError):
        trust.parse_simple_toml(text)


def test_machine_toml_round_trip_escapes_name() -> None:
    data = {
        "name": 'workstation "alpha" \\ local',
        "public_key": "age1" + ("f" * 64),
        "machine_id_hash": "0" * 64,
        "status": "trusted",
    }

    parsed = trust.parse_simple_toml(trust._render_machine(data))

    assert parsed == data


def test_machine_name_is_bounded_before_key_creation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid machine name"):
        trust.init_machine(tmp_path, name="x" * (trust.MACHINE_NAME_MAX_CHARS + 1))

    assert not trust.private_key_path(tmp_path).exists()
