from __future__ import annotations

import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ai_core import memory


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_session_note_repairs_symlink_without_mutating_external(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    external = tmp_path / "outside-session.md"
    external.write_text("EXTERNAL_SESSION\n", encoding="utf-8")
    path = memory.session_current_path(root)
    path.parent.mkdir(parents=True)
    path.symlink_to(external)

    result = memory.append_session_note(root, text="inside milestone")

    assert result["ok"] is True
    assert not path.is_symlink()
    assert "inside milestone" in path.read_text(encoding="utf-8")
    assert "EXTERNAL_SESSION" not in path.read_text(encoding="utf-8")
    assert external.read_text(encoding="utf-8") == "EXTERNAL_SESSION\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_session_note_repairs_hardlink_without_mutating_external(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    external = tmp_path / "outside-session.md"
    external.write_text("EXTERNAL_SESSION\n", encoding="utf-8")
    path = memory.session_current_path(root)
    path.parent.mkdir(parents=True)
    os.link(external, path)

    result = memory.append_session_note(root, text="inside milestone")

    assert result["ok"] is True
    assert path.stat().st_ino != external.stat().st_ino
    assert external.read_text(encoding="utf-8") == "EXTERNAL_SESSION\n"
    assert "inside milestone" in path.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name == "nt", reason="Unix directory symlink semantics")
def test_session_note_rejects_external_parent_without_mutation(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    external = tmp_path / "external-memory"
    external.mkdir()
    ai = root / ".ai"
    ai.mkdir(parents=True)
    (ai / "memory").symlink_to(external, target_is_directory=True)

    result = memory.append_session_note(root, text="inside milestone")

    assert result == {"ok": False, "reason": "write_error"}
    assert list(external.iterdir()) == []


def test_session_note_rotation_is_byte_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    monkeypatch.setattr(memory, "_SESSION_NOTE_MAX_BYTES", 2048)
    monkeypatch.setattr(memory, "_SESSION_NOTE_KEEP_BYTES", 1024)
    path = memory.session_current_path(root)
    path.parent.mkdir(parents=True)
    path.write_text("# Current Session\n\n" + ("오래된 기록\n" * 500), encoding="utf-8")

    result = memory.append_session_note(root, text="최신 마일스톤")
    raw = path.read_bytes()

    assert result["ok"] is True
    assert len(raw) <= 2048
    text = raw.decode("utf-8")
    assert "[rotated]" in text
    assert "최신 마일스톤" in text


def test_session_note_concurrent_appends_are_not_lost(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    notes = [f"parallel-note-{index}" for index in range(24)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda note: memory.append_session_note(root, text=note), notes))

    content = memory.session_current_path(root).read_text(encoding="utf-8")
    assert all(result["ok"] is True for result in results)
    for note in notes:
        assert content.count(f"] {note}\n") == 1
    if os.name != "nt":
        assert stat.S_IMODE(memory.session_current_path(root).stat().st_mode) == 0o600


def test_session_note_replaces_group_writable_file(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    path = memory.session_current_path(root)
    path.parent.mkdir(parents=True)
    path.write_text("UNTRUSTED_EXISTING\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o666)

    result = memory.append_session_note(root, text="trusted replacement")

    assert result["ok"] is True
    content = path.read_text(encoding="utf-8")
    assert "UNTRUSTED_EXISTING" not in content
    assert "trusted replacement" in content
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
