from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core import doctor  # noqa: E402
from ai_core.preflight_proof import PROOF_SCHEMA, environment_fingerprint  # noqa: E402


def _write_proof(repo: Path, *, age_seconds: float = 0) -> Path:
    script = repo / "scripts" / "preflight.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    proof_path = repo / ".ai" / "cache" / "preflight-proof.json"
    proof_path.parent.mkdir(parents=True)
    resolved = repo.resolve()
    proof_path.write_text(
        json.dumps(
            {
                "schema": PROOF_SCHEMA,
                "ok": True,
                "created_at_unix": time.time() - age_seconds,
                "preflight_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
                "root_fingerprint": hashlib.sha256(str(resolved).encode("utf-8")).hexdigest(),
                "environment_fingerprint": environment_fingerprint(resolved),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    if os.name != "nt":
        proof_path.chmod(0o600)
    return proof_path


def test_doctor_uses_valid_fresh_preflight_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_proof(tmp_path)

    def unexpected_subprocess(*_args, **_kwargs):
        raise AssertionError("valid proof must avoid repeating preflight")

    monkeypatch.setattr(doctor.subprocess, "run", unexpected_subprocess)

    check = doctor.check_bootstrap_preflight(tmp_path)

    assert check.ok is True
    assert check.detail == "ok (fresh bootstrap proof)"


def test_doctor_accepts_environment_bound_proof_older_than_five_minutes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_proof(tmp_path, age_seconds=600)

    def unexpected_subprocess(*_args, **_kwargs):
        raise AssertionError("unchanged environment proof should remain valid for one hour")

    monkeypatch.setattr(doctor.subprocess, "run", unexpected_subprocess)

    check = doctor.check_bootstrap_preflight(tmp_path)

    assert check.ok is True


def test_doctor_rechecks_when_preflight_environment_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_proof(tmp_path)
    monkeypatch.setenv("UV_OFFLINE", "1")
    calls: list[list[str]] = []

    def failed_preflight(command, **_kwargs):
        calls.append([str(item) for item in command])
        return doctor.subprocess.CompletedProcess(command, 1, stdout="", stderr="environment changed")

    monkeypatch.setattr(doctor.subprocess, "run", failed_preflight)

    check = doctor.check_bootstrap_preflight(tmp_path)

    assert calls
    assert check.ok is False
    assert check.detail == "environment changed"


@pytest.mark.skipif(os.name == "nt" or not hasattr(os, "geteuid"), reason="POSIX owner check")
def test_doctor_rechecks_preflight_proof_not_owned_by_effective_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_proof(tmp_path)
    current_uid = os.geteuid()
    monkeypatch.setattr(doctor.os, "geteuid", lambda: current_uid + 1)
    calls: list[list[str]] = []

    def failed_preflight(command, **_kwargs):
        calls.append([str(item) for item in command])
        return doctor.subprocess.CompletedProcess(command, 1, stdout="", stderr="owner mismatch")

    monkeypatch.setattr(doctor.subprocess, "run", failed_preflight)

    check = doctor.check_bootstrap_preflight(tmp_path)

    assert calls
    assert check.ok is False
    assert check.detail == "owner mismatch"


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_doctor_rechecks_hardlinked_preflight_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = _write_proof(tmp_path)
    content = proof.read_text(encoding="utf-8")
    proof.unlink()
    external = tmp_path / "external-proof.json"
    external.write_text(content, encoding="utf-8")
    if os.name != "nt":
        external.chmod(0o600)
    os.link(external, proof)
    calls: list[list[str]] = []

    def failed_preflight(command, **_kwargs):
        calls.append([str(item) for item in command])
        return doctor.subprocess.CompletedProcess(command, 1, stdout="", stderr="hardlink proof rejected")

    monkeypatch.setattr(doctor.subprocess, "run", failed_preflight)

    check = doctor.check_bootstrap_preflight(tmp_path)

    assert calls
    assert check.ok is False
    assert check.detail == "hardlink proof rejected"
    assert external.read_text(encoding="utf-8") == content


def test_doctor_rechecks_when_preflight_proof_script_hash_is_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_proof(tmp_path)
    (tmp_path / "scripts" / "preflight.sh").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    calls: list[list[str]] = []

    def failed_preflight(command, **_kwargs):
        calls.append([str(item) for item in command])
        return doctor.subprocess.CompletedProcess(command, 1, stdout="", stderr="stale proof rejected")

    monkeypatch.setattr(doctor.subprocess, "run", failed_preflight)

    check = doctor.check_bootstrap_preflight(tmp_path)

    assert calls
    assert check.ok is False
    assert check.detail == "stale proof rejected"


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_doctor_never_executes_external_preflight_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    external = tmp_path.with_name(tmp_path.name + "-external-preflight.sh")
    external.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    external.chmod(0o755)
    script = tmp_path / "scripts" / "preflight.sh"
    script.parent.mkdir(parents=True)
    script.symlink_to(external)

    def unexpected_subprocess(*_args, **_kwargs):
        raise AssertionError("external preflight symlink must never execute")

    monkeypatch.setattr(doctor.subprocess, "run", unexpected_subprocess)

    check = doctor.check_bootstrap_preflight(tmp_path)

    assert check.ok is False
    assert check.detail == "scripts/preflight.sh must be a root-confined regular file"


def test_doctor_rejects_preflight_directory(tmp_path: Path) -> None:
    (tmp_path / "scripts" / "preflight.sh").mkdir(parents=True)

    check = doctor.check_bootstrap_preflight(tmp_path)

    assert check.ok is False
    assert check.detail == "scripts/preflight.sh must be a root-confined regular file"