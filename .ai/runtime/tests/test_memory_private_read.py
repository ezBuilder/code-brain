from __future__ import annotations

import os
from pathlib import Path

import pytest

from ai_core.memory import (
    close_todo,
    read_jsonl_all,
    read_jsonl_open_todos,
    read_jsonl_tail,
    read_text_tail,
)


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_common_memory_readers_never_follow_external_symlink(tmp_path: Path) -> None:
    external = tmp_path / "external.jsonl"
    external.write_text(
        '{"id":"x","status":"open","title":"EXTERNAL_INJECTION"}\n',
        encoding="utf-8",
    )
    path = tmp_path / ".ai" / "memory" / "todos.jsonl"
    path.parent.mkdir(parents=True)
    path.symlink_to(external)

    assert read_jsonl_tail(path, 5) == []
    assert read_jsonl_open_todos(path, 5) == []
    assert read_jsonl_all(path) == []
    assert read_text_tail(path, 5) == ""
    assert close_todo(tmp_path, match="EXTERNAL_INJECTION") == {
        "ok": False,
        "reason": "no_todos",
    }
    assert "EXTERNAL_INJECTION" in external.read_text(encoding="utf-8")


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_common_memory_readers_never_read_external_hardlink(tmp_path: Path) -> None:
    external = tmp_path / "external.jsonl"
    external.write_text('{"id":"x","decision":"EXTERNAL_HARDLINK"}\n', encoding="utf-8")
    path = tmp_path / ".ai" / "memory" / "decisions.jsonl"
    path.parent.mkdir(parents=True)
    os.link(external, path)

    assert read_jsonl_tail(path, 5) == []
    assert read_jsonl_all(path) == []
    assert read_text_tail(path, 5) == ""
    assert "EXTERNAL_HARDLINK" in external.read_text(encoding="utf-8")


def test_common_memory_readers_preserve_normal_behavior(tmp_path: Path) -> None:
    todos = tmp_path / ".ai" / "memory" / "todos.jsonl"
    todos.parent.mkdir(parents=True)
    todos.write_text(
        '{"id":"a","status":"open","title":"first"}\n'
        '{"id":"b","status":"done","title":"second"}\n',
        encoding="utf-8",
    )
    text = tmp_path / ".ai" / "memory" / "session-current.md"
    text.write_text("one\ntwo\nthree\n", encoding="utf-8")

    assert [row["id"] for row in read_jsonl_all(todos)] == ["a", "b"]
    assert [row["id"] for row in read_jsonl_tail(todos, 1)] == ["b"]
    assert [row["id"] for row in read_jsonl_open_todos(todos, 5)] == ["a"]
    assert read_text_tail(text, 2) == "two\nthree"
