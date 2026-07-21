from __future__ import annotations

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ai_core import memory
from ai_core.loss_accounting import summary as loss_summary
from ai_core.memory import (
    append_jsonl,
    append_session_note,
    jsonl_lock_path,
    session_current_path,
    state_root_for_path,
)


def test_append_jsonl_creates_private_file_and_lock(tmp_path: Path) -> None:
    path = tmp_path / ".ai" / "memory" / "records.jsonl"

    append_jsonl(path, {"id": 1})

    assert json.loads(path.read_text(encoding="utf-8")) == {"id": 1}
    assert state_root_for_path(path) == tmp_path
    assert jsonl_lock_path(path).is_file()
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(jsonl_lock_path(path).stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_append_jsonl_refuses_external_symlink(tmp_path: Path) -> None:
    path = tmp_path / ".ai" / "memory" / "records.jsonl"
    path.parent.mkdir(parents=True)
    external = tmp_path / "external.jsonl"
    external.write_text('{"external":true}\n', encoding="utf-8")
    path.symlink_to(external)

    with pytest.raises(OSError):
        append_jsonl(path, {"id": 1})

    assert external.read_text(encoding="utf-8") == '{"external":true}\n'


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_append_jsonl_refuses_external_hardlink_without_mode_change(tmp_path: Path) -> None:
    path = tmp_path / ".ai" / "memory" / "records.jsonl"
    path.parent.mkdir(parents=True)
    external = tmp_path / "external.jsonl"
    external.write_text('{"external":true}\n', encoding="utf-8")
    original_mode = stat.S_IMODE(external.stat().st_mode)
    os.link(external, path)

    with pytest.raises(OSError, match="hard links"):
        append_jsonl(path, {"id": 1})

    assert external.read_text(encoding="utf-8") == '{"external":true}\n'
    assert stat.S_IMODE(external.stat().st_mode) == original_mode


def test_append_jsonl_concurrent_records_are_complete_and_not_lost(tmp_path: Path) -> None:
    path = tmp_path / ".ai" / "memory" / "records.jsonl"

    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(append_jsonl, path, {"id": index}) for index in range(100)]
        for future in futures:
            future.result()

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 100
    assert {row["id"] for row in rows} == set(range(100))


def test_session_note_rotation_is_private_atomic_and_accounted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(memory, "_SESSION_NOTE_MAX_BYTES", 200)
    monkeypatch.setattr(memory, "_SESSION_NOTE_KEEP_BYTES", 80)
    path = session_current_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("# Current Session\n\n" + "old-line\n" * 80, encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    before = path.stat().st_size

    result = append_session_note(tmp_path, text="newest milestone")

    assert result["ok"] is True
    assert result["rotation_loss"] is not None
    assert result["rotation_loss"]["bytes"]["removed"] > 0
    assert path.stat().st_size < before
    assert "newest milestone" in path.read_text(encoding="utf-8")
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    accounting = loss_summary(tmp_path)["domains"]["session_rotation"]
    assert accounting["removed_bytes"] == result["rotation_loss"]["bytes"]["removed"]


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_session_note_refuses_external_symlink(tmp_path: Path) -> None:
    path = session_current_path(tmp_path)
    path.parent.mkdir(parents=True)
    external = tmp_path / "external-session.md"
    external.write_text("external\n", encoding="utf-8")
    path.symlink_to(external)

    result = append_session_note(tmp_path, text="must-not-escape")

    assert result["ok"] is False
    assert external.read_text(encoding="utf-8") == "external\n"


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_session_note_refuses_external_hardlink(tmp_path: Path) -> None:
    path = session_current_path(tmp_path)
    path.parent.mkdir(parents=True)
    external = tmp_path / "external-session.md"
    external.write_text("external\n", encoding="utf-8")
    if os.name != "nt":
        external.chmod(0o600)
    os.link(external, path)

    result = append_session_note(tmp_path, text="must-not-escape")

    assert result["ok"] is False
    assert external.read_text(encoding="utf-8") == "external\n"


def test_session_note_concurrent_appends_are_not_lost(tmp_path: Path) -> None:
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda index: append_session_note(tmp_path, text=f"note-{index}"), range(30)))

    assert all(result["ok"] for result in results)
    text = session_current_path(tmp_path).read_text(encoding="utf-8")
    for index in range(30):
        assert f"note-{index}" in text