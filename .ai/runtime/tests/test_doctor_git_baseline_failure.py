from __future__ import annotations

from pathlib import Path

import pytest

from ai_core import doctor
from ai_core import tracked_files


def test_strict_secret_scan_fails_explicitly_when_git_baseline_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".git").mkdir()
    local_noise = tmp_path / ".chatgpt2codex" / "session.json"
    local_noise.parent.mkdir()
    local_noise.write_text("token=" + "x" * 24 + "\n", encoding="utf-8")

    def unavailable_git(*_args, **_kwargs):
        raise OSError("git unavailable")

    monkeypatch.setattr(tracked_files.subprocess, "run", unavailable_git)

    check = doctor.check_secret_scan(
        tmp_path,
        incremental=False,
        update_state=False,
    )

    assert check.ok is False
    assert "Git tracked-file baseline unavailable" in check.detail
    assert "mode=full" in check.detail
    assert ".chatgpt2codex" not in check.detail


def test_non_git_secret_scan_uses_safe_filesystem_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "src" / "main.py"
    source.parent.mkdir()
    source.write_text("VALUE = 1\n", encoding="utf-8")
    noise = tmp_path / ".chatgpt2codex" / "session.json"
    noise.parent.mkdir()
    noise.write_text("token=" + "x" * 24 + "\n", encoding="utf-8")

    def unavailable_git(*_args, **_kwargs):
        raise OSError("not a Git repository")

    monkeypatch.setattr(tracked_files.subprocess, "run", unavailable_git)
    check = doctor.check_secret_scan(tmp_path, incremental=False, update_state=False)

    assert check.ok is True
    assert "baseline=filesystem" in check.detail
    assert "total=1" in check.detail


def test_incremental_secret_scan_uses_valid_cache_during_temporary_git_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    source = repo / "safe.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "safe.py"], cwd=repo, check=True)
    full = doctor.check_secret_scan(repo, incremental=False, update_state=True)
    assert full.ok is True

    def unavailable_git(*_args, **_kwargs):
        raise AssertionError("incremental scan should use the trusted tracked cache")

    monkeypatch.setattr(tracked_files.subprocess, "run", unavailable_git)
    incremental = doctor.check_secret_scan(repo, incremental=True, update_state=False)

    assert incremental.ok is True
    assert "mode=incremental" in incremental.detail
    assert "reused=1" in incremental.detail