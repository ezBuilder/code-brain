from __future__ import annotations

import hashlib
import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ai_core.worker import ipc


def _machine_file(root: Path, machine_hash: str, text: str) -> Path:
    path = root / ".ai" / "trust" / "machines" / f"{machine_hash}.pub.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_worker_token_repairs_symlink_without_mutating_external(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ipc, "is_ci", lambda: False)
    root = tmp_path / "repo"
    external = tmp_path / "outside.token"
    external.write_text("a" * ipc.TOKEN_HEX_CHARS + "\n", encoding="utf-8")
    path = ipc.token_path(root)
    path.parent.mkdir(parents=True)
    path.symlink_to(external)

    token = ipc.get_or_create_token(root)

    assert ipc._TOKEN_RE.fullmatch(token)
    assert not path.is_symlink()
    assert path.read_text(encoding="utf-8").strip() == token
    assert external.read_text(encoding="utf-8") == "a" * ipc.TOKEN_HEX_CHARS + "\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_worker_token_repairs_hardlink_without_mutating_external(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ipc, "is_ci", lambda: False)
    root = tmp_path / "repo"
    external = tmp_path / "outside.token"
    external.write_text("b" * ipc.TOKEN_HEX_CHARS + "\n", encoding="utf-8")
    path = ipc.token_path(root)
    path.parent.mkdir(parents=True)
    os.link(external, path)

    token = ipc.get_or_create_token(root)

    assert ipc._TOKEN_RE.fullmatch(token)
    assert path.stat().st_ino != external.stat().st_ino
    assert external.read_text(encoding="utf-8") == "b" * ipc.TOKEN_HEX_CHARS + "\n"


def test_worker_token_concurrent_creation_returns_single_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ipc, "is_ci", lambda: False)
    root = tmp_path / "repo"

    with ThreadPoolExecutor(max_workers=12) as pool:
        tokens = list(pool.map(lambda _index: ipc.get_or_create_token(root), range(40)))

    assert len(set(tokens)) == 1
    assert ipc.token_path(root).read_text(encoding="utf-8").strip() == tokens[0]


def test_invalid_or_public_worker_token_is_replaced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ipc, "is_ci", lambda: False)
    root = tmp_path / "repo"
    path = ipc.token_path(root)
    path.parent.mkdir(parents=True)
    path.write_text("not-a-token\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o644)

    token = ipc.get_or_create_token(root)

    assert ipc._TOKEN_RE.fullmatch(token)
    assert token != "not-a-token"
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_ci_uses_fixed_token_without_writing_when_token_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ipc, "is_ci", lambda: True)
    root = tmp_path / "repo"

    token = ipc.get_or_create_token(root)

    assert token == ipc.CI_READONLY_TOKEN
    assert not ipc.token_path(root).exists()


def test_machine_hash_uses_only_canonical_trusted_files(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    first_hash = "1" * 64
    second_hash = "2" * 64
    first = _machine_file(root, first_hash, "name = \"one\"\n")
    second = _machine_file(root, second_hash, "name = \"two\"\n")
    (first.parent / "not-a-machine.pub.toml").write_text("ignored\n", encoding="utf-8")
    expected = hashlib.sha256(
        (first.read_text(encoding="utf-8") + "\n" + second.read_text(encoding="utf-8")).encode("utf-8")
    ).hexdigest()

    assert ipc.machine_id_hash(root) == expected


@pytest.mark.skipif(os.name == "nt", reason="Unix parent symlink semantics")
def test_machine_hash_does_not_traverse_external_parent(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    external = tmp_path / "outside-machines"
    external.mkdir()
    outside = external / (("3" * 64) + ".pub.toml")
    outside.write_text("outside secret\n", encoding="utf-8")
    trust = root / ".ai" / "trust"
    trust.mkdir(parents=True)
    (trust / "machines").symlink_to(external, target_is_directory=True)

    assert ipc.machine_id_hash(root) == hashlib.sha256(b"").hexdigest()
    assert outside.read_text(encoding="utf-8") == "outside secret\n"


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_machine_hash_ignores_linked_machine_file(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    external = tmp_path / "outside-machine.toml"
    external.write_text("outside secret\n", encoding="utf-8")
    target = root / ".ai" / "trust" / "machines" / (("4" * 64) + ".pub.toml")
    target.parent.mkdir(parents=True)
    target.symlink_to(external)

    assert ipc.machine_id_hash(root) == hashlib.sha256(b"").hexdigest()


def test_validate_envelope_checks_all_identity_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ipc, "is_ci", lambda: True)
    root = tmp_path / "repo"
    root.mkdir()
    envelope = ipc.build_envelope(root, request_id="req")
    ipc.validate_envelope(root, envelope)

    for field, value, code in (
        ("root_id", "wrong", "UNAUTHORIZED"),
        ("root_hash", "0" * 64, "UNAUTHORIZED"),
        ("machine_id_hash", "0" * 64, "UNAUTHORIZED"),
        ("request_id", "", "INVALID_REQUEST"),
    ):
        modified = dict(envelope)
        modified[field] = value
        with pytest.raises(ipc.IpcError) as exc_info:
            ipc.validate_envelope(root, modified)
        assert exc_info.value.code == code


def test_validate_envelope_uses_constant_time_token_compare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ipc, "is_ci", lambda: True)
    root = tmp_path / "repo"
    root.mkdir()
    envelope = ipc.build_envelope(root)
    calls: list[tuple[str, str]] = []
    real_compare = ipc.secrets.compare_digest

    def recording_compare(left: str, right: str) -> bool:
        calls.append((left, right))
        return real_compare(left, right)

    monkeypatch.setattr(ipc.secrets, "compare_digest", recording_compare)
    ipc.validate_envelope(root, envelope)

    assert (envelope["token"], ipc.CI_READONLY_TOKEN) in calls
    assert (envelope["root_hash"], ipc.root_hash(root)) in calls
    assert (envelope["machine_id_hash"], ipc.machine_id_hash(root)) in calls


def test_parse_envelope_is_bounded_and_requires_object(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ipc, "ENVELOPE_MAX_BYTES", 64)

    with pytest.raises(ipc.IpcError, match="invalid worker envelope"):
        ipc.parse_envelope("x" * 65)
    with pytest.raises(ipc.IpcError, match="invalid worker envelope"):
        ipc.parse_envelope("[]")
    with pytest.raises(ipc.IpcError, match="invalid worker envelope"):
        ipc.parse_envelope("{not-json")

    assert ipc.parse_envelope(None) is None
    assert ipc.parse_envelope(json.dumps({"protocol_version": 1})) == {"protocol_version": 1}


def test_build_envelope_caps_request_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ipc, "is_ci", lambda: True)
    root = tmp_path / "repo"
    root.mkdir()

    envelope = ipc.build_envelope(root, request_id="x" * 1000)

    assert len(envelope["request_id"]) == ipc.REQUEST_ID_MAX_CHARS
