from __future__ import annotations

import os
from pathlib import Path

import pytest

from ai_core import doctor, obs
from ai_core.retention import prune_directory, retention_status


def test_prune_directory_enforces_age_count_and_bytes(tmp_path: Path) -> None:
    directory = tmp_path / ".ai" / "cache" / "items"
    directory.mkdir(parents=True)
    files = []
    for index in range(5):
        path = directory / f"item-{index}.json"
        path.write_bytes((str(index) * 100).encode("utf-8"))
        timestamp = 1_700_000_000 + index
        os.utime(path, (timestamp, timestamp))
        files.append(path)

    result = prune_directory(
        tmp_path,
        directory,
        prefixes=("item-",),
        suffixes=(".json",),
        keep_days=100_000,
        max_files=2,
        max_bytes=250,
        now=1_700_000_100,
    )

    assert result["ok"] is True
    assert result["removed_count"] == 3
    assert [path.name for path in directory.glob("*.json")] == ["item-3.json", "item-4.json"]
    status = retention_status(
        tmp_path,
        directory,
        prefixes=("item-",),
        suffixes=(".json",),
        keep_days=100_000,
        max_files=2,
        max_bytes=250,
        now=1_700_000_100,
    )
    assert status["ok"] is True
    assert status["count"] == 2
    assert status["bytes"] == 200


def test_prune_directory_preserves_requested_new_backup(tmp_path: Path) -> None:
    directory = tmp_path / ".ai" / "cache" / "upgrade"
    directory.mkdir(parents=True)
    old = directory / "rollback-old.json"
    current = directory / "rollback-current.json"
    old.write_text("old", encoding="utf-8")
    current.write_text("current", encoding="utf-8")
    os.utime(old, (1, 1))
    os.utime(current, (1, 1))

    result = prune_directory(
        tmp_path,
        directory,
        prefixes=("rollback-",),
        suffixes=(".json",),
        keep_days=100_000,
        max_files=1,
        max_bytes=1024,
        preserve=(current,),
        now=10,
    )

    assert result["ok"] is True
    assert current.exists()
    assert not old.exists()


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_retention_refuses_matching_symlink_without_deleting_target(tmp_path: Path) -> None:
    directory = tmp_path / ".ai" / "cache" / "diagnostics"
    directory.mkdir(parents=True)
    external = tmp_path / "external.zip"
    external.write_bytes(b"external")
    link = directory / "diagnostics-unsafe.zip"
    link.symlink_to(external)

    result = prune_directory(
        tmp_path,
        directory,
        prefixes=("diagnostics-",),
        suffixes=(".zip",),
        keep_days=0,
        max_files=0,
        max_bytes=0,
        now=10,
    )

    assert result["ok"] is False
    assert external.read_bytes() == b"external"
    assert link.is_symlink()
    assert any("unsafe-symlink" in error for error in result["errors"])


def test_write_log_bounds_payload_and_prunes_old_daily_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(obs, "LOG_MAX_FILES", 2)
    monkeypatch.setattr(obs, "LOG_PAYLOAD_MAX_BYTES", 200)
    monkeypatch.setattr(obs, "LOG_RETENTION_DAYS", 100_000)
    logs = tmp_path / ".ai" / "cache" / "logs"
    logs.mkdir(parents=True)
    for index in range(4):
        path = logs / f"2000-01-0{index + 1}.jsonl"
        path.write_text('{"old":true}\n', encoding="utf-8")
        os.utime(path, (index + 1, index + 1))

    result = obs.write_log(tmp_path, "info", "test", {"blob": "x" * 10_000})

    assert result["ok"] is True
    assert result["record"]["payload"]["truncated"] is True
    assert len(list(logs.glob("*.jsonl"))) == 2


def test_doctor_rejects_runtime_retention_overflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(obs, "DIAGNOSTICS_MAX_FILES", 1)
    diagnostics = tmp_path / ".ai" / "cache" / "diagnostics"
    diagnostics.mkdir(parents=True)
    for index in range(2):
        (diagnostics / f"diagnostics-{index}.zip").write_bytes(b"zip")

    check = doctor.check_runtime_retention(tmp_path)

    assert check.ok is False
    assert "diagnostics:files=2>1" in check.detail
