from __future__ import annotations

from pathlib import Path

import pytest

from ai_core import doctor


def test_scan_candidates_skip_file_deleted_after_git_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "tracked.py"
    path.write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(doctor, "secret_scan_files", lambda _root, **_kwargs: [path])
    path.unlink()

    assert doctor._secret_scan_candidates(tmp_path) == []


def test_scan_candidates_keep_regular_small_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "tracked.py"
    path.write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(doctor, "secret_scan_files", lambda _root, **_kwargs: [path])

    assert doctor._secret_scan_candidates(tmp_path) == [path]


def test_scan_candidates_skip_directory_and_large_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "tracked-dir"
    directory.mkdir()
    large = tmp_path / "large.txt"
    large.write_bytes(b"x" * 1_000_001)
    monkeypatch.setattr(doctor, "secret_scan_files", lambda _root, **_kwargs: [directory, large])

    assert doctor._secret_scan_candidates(tmp_path) == []


@pytest.mark.skipif(__import__("os").name == "nt", reason="Unix symlink semantics")
def test_scan_candidates_keep_broken_symlink_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    linked = tmp_path / "credential-link"
    linked.symlink_to("token=" + "q" * 24)
    monkeypatch.setattr(doctor, "secret_scan_files", lambda _root, **_kwargs: [linked])

    assert doctor._secret_scan_candidates(tmp_path) == [linked]