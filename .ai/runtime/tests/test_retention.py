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


def test_prune_directory_dry_run_fails_when_preserved_files_exceed_count_limit(
    tmp_path: Path,
) -> None:
    directory = tmp_path / ".ai" / "cache" / "upgrade"
    directory.mkdir(parents=True)
    first = directory / "rollback-first.json"
    second = directory / "rollback-second.json"
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    result = prune_directory(
        tmp_path,
        directory,
        prefixes=("rollback-",),
        suffixes=(".json",),
        keep_days=100_000,
        max_files=1,
        max_bytes=1024,
        preserve=(first, second),
        dry_run=True,
        now=10,
    )

    assert result["ok"] is False
    assert result["status"]["projected"] is True
    assert result["status"]["count"] == 2
    assert result["status"]["actual_count"] == 2
    assert "files=2>1" in result["status"]["violations"]
    assert first.exists() and second.exists()


def test_prune_directory_dry_run_fails_when_preserved_file_is_expired_or_oversized(
    tmp_path: Path,
) -> None:
    directory = tmp_path / ".ai" / "cache" / "upgrade"
    directory.mkdir(parents=True)
    preserved = directory / "rollback-current.json"
    preserved.write_bytes(b"x" * 100)
    os.utime(preserved, (1, 1))

    result = prune_directory(
        tmp_path,
        directory,
        prefixes=("rollback-",),
        suffixes=(".json",),
        keep_days=1,
        max_files=1,
        max_bytes=50,
        preserve=(preserved,),
        dry_run=True,
        now=200_000,
    )

    assert result["ok"] is False
    assert "expired=1" in result["status"]["violations"]
    assert "bytes=100>50" in result["status"]["violations"]
    assert result["removed_count"] == 0
    assert preserved.exists()


def test_prune_directory_dry_run_reports_projected_success_without_mutation(
    tmp_path: Path,
) -> None:
    directory = tmp_path / ".ai" / "cache" / "logs"
    directory.mkdir(parents=True)
    old = directory / "old.jsonl"
    current = directory / "current.jsonl"
    old.write_bytes(b"o" * 100)
    current.write_bytes(b"c" * 100)
    os.utime(old, (1, 1))
    os.utime(current, (2, 2))

    result = prune_directory(
        tmp_path,
        directory,
        suffixes=(".jsonl",),
        keep_days=100_000,
        max_files=1,
        max_bytes=150,
        dry_run=True,
        now=10,
    )

    assert result["ok"] is True
    assert result["status"]["violations"] == []
    assert result["status"]["count"] == 1
    assert result["status"]["bytes"] == 100
    assert result["status"]["actual_count"] == 2
    assert result["removed"] == ["old.jsonl"]
    assert old.exists() and current.exists()


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


def test_doctor_jsonl_validation_streams_without_path_read_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / ".ai" / "memory" / "events" / "events.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text('{"ok":true}\n' * 1000, encoding="utf-8")
    original = Path.read_text

    def guarded_read_text(self: Path, *args, **kwargs):
        if self.suffix == ".jsonl":
            raise AssertionError("JSONL validation must stream instead of read_text")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    check = doctor.check_jsonl(tmp_path)

    assert check.ok is True
    assert "files=1" in check.detail


def test_doctor_jsonl_rejects_oversized_line_without_materializing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor, "JSONL_CHECK_MAX_LINE_BYTES", 64)
    path = tmp_path / ".ai" / "memory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text('{"blob":"' + ("x" * 1000) + '"}\n{"ok":true}\n', encoding="utf-8")

    check = doctor.check_jsonl(tmp_path)

    assert check.ok is False
    assert "events.jsonl:1:line-byte-limit" in check.detail


def test_doctor_jsonl_enforces_aggregate_scan_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor, "JSONL_CHECK_MAX_TOTAL_BYTES", 20)
    memory = tmp_path / ".ai" / "memory"
    memory.mkdir(parents=True)
    (memory / "a.jsonl").write_text('{"value":1}\n', encoding="utf-8")
    (memory / "b.jsonl").write_text('{"value":2}\n', encoding="utf-8")

    check = doctor.check_jsonl(tmp_path)

    assert check.ok is False
    assert "aggregate-bytes=" in check.detail
