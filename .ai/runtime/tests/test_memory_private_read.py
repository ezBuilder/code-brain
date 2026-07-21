from __future__ import annotations

import os
from pathlib import Path

import pytest

from ai_core import memory

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


def test_tail_readers_do_not_use_whole_state_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / ".ai" / "memory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text('{"id":"a"}\n{"id":"b"}\n', encoding="utf-8")
    text = tmp_path / ".ai" / "memory" / "session-current.md"
    text.write_text("one\ntwo\nthree\n", encoding="utf-8")

    def unexpected_whole_read(*_args, **_kwargs):
        raise AssertionError("tail readers must not materialize the whole file")

    monkeypatch.setattr(memory, "read_state_text", unexpected_whole_read)

    assert [row["id"] for row in read_jsonl_tail(path, 1)] == ["b"]
    assert read_text_tail(text, 2) == "two\nthree"


def test_tail_readers_skip_large_prefix_with_bounded_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(memory, "MEMORY_TAIL_MIN_SCAN_BYTES", 64)
    monkeypatch.setattr(memory, "MEMORY_TAIL_MAX_SCAN_BYTES", 64)
    monkeypatch.setattr(memory, "MEMORY_TAIL_BYTES_PER_ITEM", 16)
    path = tmp_path / ".ai" / "memory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        ("x" * 10_000)
        + "\n"
        + '{"id":"latest-a"}\n'
        + '{"id":"latest-b"}\n',
        encoding="utf-8",
    )
    text = tmp_path / ".ai" / "memory" / "session-current.md"
    text.write_text(("x" * 10_000) + "\nlatest-a\nlatest-b\n", encoding="utf-8")

    assert [row["id"] for row in read_jsonl_tail(path, 2)] == ["latest-a", "latest-b"]
    assert read_text_tail(text, 2) == "latest-a\nlatest-b"


def test_state_jsonl_readers_and_close_todo_stream_without_whole_state_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    todos = tmp_path / ".ai" / "memory" / "todos.jsonl"
    todos.parent.mkdir(parents=True)
    todos.write_text(
        '{"id":"a","status":"open","title":"first"}\n'
        '{"id":"b","status":"open","title":"second"}\n',
        encoding="utf-8",
    )

    def unexpected_whole_read(*_args, **_kwargs):
        raise AssertionError("state JSONL readers must stream")

    monkeypatch.setattr(memory, "read_state_text", unexpected_whole_read)

    assert [row["id"] for row in read_jsonl_all(todos)] == ["a", "b"]
    assert [row["id"] for row in read_jsonl_open_todos(todos, 5)] == ["a", "b"]
    result = close_todo(tmp_path, match="second", status="done")
    assert result["ok"] is True
    assert result["record"]["id"] == "b"
    assert [row["id"] for row in read_jsonl_open_todos(todos, 5)] == ["a"]


@pytest.mark.parametrize(
    ("constant", "value", "content"),
    [
        ("STATE_JSONL_MAX_BYTES", 16, '{"id":"a","status":"open","title":"' + ("x" * 100) + '"}\n'),
        ("STATE_JSONL_MAX_LINE_BYTES", 32, '{"id":"a","status":"open","title":"' + ("x" * 100) + '"}\n'),
        (
            "STATE_JSONL_MAX_RECORDS",
            1,
            '{"id":"a","status":"open","title":"first"}\n'
            '{"id":"b","status":"open","title":"second"}\n',
        ),
    ],
)
def test_state_jsonl_limits_fail_soft_and_close_todo_does_not_mutate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
    value: int,
    content: str,
) -> None:
    todos = tmp_path / ".ai" / "memory" / "todos.jsonl"
    todos.parent.mkdir(parents=True)
    todos.write_text(content, encoding="utf-8")
    original = todos.read_bytes()
    monkeypatch.setattr(memory, constant, value)

    assert read_jsonl_all(todos) == []
    assert read_jsonl_open_todos(todos, 5) == []
    assert close_todo(tmp_path, match="first", status="done") == {
        "ok": False,
        "reason": "no_todos",
    }
    assert todos.read_bytes() == original


def test_state_jsonl_reader_skips_malformed_rows_without_losing_valid_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".ai" / "memory" / "decisions.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"id":"a"}\n'
        'not-json\n'
        '["not", "an", "object"]\n'
        '{"id":"b"}\n',
        encoding="utf-8",
    )

    assert [row["id"] for row in read_jsonl_all(path)] == ["a", "b"]


def test_close_todo_handles_legacy_title_without_id(tmp_path: Path) -> None:
    todos = tmp_path / ".ai" / "memory" / "todos.jsonl"
    todos.parent.mkdir(parents=True)
    todos.write_text(
        '{"status":"open","title":"legacy task"}\n',
        encoding="utf-8",
    )

    result = close_todo(tmp_path, match="legacy task", status="done")

    assert result["ok"] is True
    assert result["record"]["id"] == "legacy:legacy task"
    assert read_jsonl_open_todos(todos, 5) == []
