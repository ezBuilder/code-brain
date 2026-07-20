from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ai_core import memory
from ai_core.private_write import iter_root_confined_text_lines


def _line(index: int, **extra) -> str:
    return json.dumps({"id": index, **extra}, separators=(",", ":")) + "\n"


def test_read_jsonl_all_streams_without_full_file_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    path = root / ".ai" / "memory" / "records.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("".join(_line(i, payload="x" * 1000) for i in range(5000)), encoding="utf-8")
    monkeypatch.setattr(
        memory,
        "read_state_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("full JSONL reader must stream")
        ),
    )

    rows = memory.read_jsonl_all(path)

    assert len(rows) == 5000
    assert rows[0]["id"] == 0
    assert rows[-1]["id"] == 4999


def test_open_todos_streams_and_preserves_last_write_wins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    path = root / ".ai" / "memory" / "todos.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        _line(1, title="first", status="open")
        + _line(2, title="second", status="open")
        + _line(1, title="first", status="done")
        + _line(3, title="third", status="open"),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        memory,
        "read_state_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("todo reader must stream")
        ),
    )

    rows = memory.read_jsonl_open_todos(path, 10)

    assert [row["id"] for row in rows] == [2, 3]


def test_streaming_reader_fails_closed_above_record_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    path = root / ".ai" / "memory" / "records.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("".join(_line(i) for i in range(6)), encoding="utf-8")
    monkeypatch.setattr(memory, "_JSONL_ALL_MAX_RECORDS", 5)

    assert memory.read_jsonl_all(path) == []
    assert memory.read_jsonl_open_todos(path, 5) == []


def test_streaming_reader_rejects_oversized_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    path = root / ".ai" / "memory" / "records.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(_line(1) + _line(2, payload="x" * 1000), encoding="utf-8")
    monkeypatch.setattr(memory, "_JSONL_LINE_MAX_BYTES", 128)

    assert memory.read_jsonl_all(path) == []


def test_streaming_reader_rejects_file_above_total_byte_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    path = root / ".ai" / "memory" / "records.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("".join(_line(i) for i in range(20)), encoding="utf-8")
    monkeypatch.setattr(memory, "_JSONL_ALL_MAX_BYTES", 64)

    assert memory.read_jsonl_all(path) == []


def test_open_todo_limit_is_bounded_and_invalid_limit_reads_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    path = root / ".ai" / "memory" / "todos.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("".join(_line(i, status="open") for i in range(1100)), encoding="utf-8")

    rows = memory.read_jsonl_open_todos(path, 10**100)

    assert len(rows) == memory._OPEN_TODO_MAX_LIMIT
    monkeypatch.setattr(
        memory,
        "iter_root_confined_text_lines",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid limit must stop before reading")
        ),
    )
    assert memory.read_jsonl_open_todos(path, "bad") == []
    assert memory.read_jsonl_open_todos(path, -1) == []


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_streaming_reader_rejects_linked_source(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    external = tmp_path / "outside.jsonl"
    external.write_text(_line(1, secret="outside"), encoding="utf-8")
    path = root / ".ai" / "memory" / "records.jsonl"
    path.parent.mkdir(parents=True)
    path.symlink_to(external)

    assert memory.read_jsonl_all(path) == []
    assert external.read_text(encoding="utf-8") == _line(1, secret="outside")


def test_confined_line_iterator_streams_utf8_and_preserves_newlines(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    path = root / ".ai" / "memory" / "lines.txt"
    path.parent.mkdir(parents=True)
    path.write_text("첫째\n둘째\n마지막", encoding="utf-8")

    lines = list(
        iter_root_confined_text_lines(
            path,
            root=root,
            max_bytes=1024,
            max_line_bytes=128,
            require_private=False,
        )
    )

    assert lines == ["첫째\n", "둘째\n", "마지막"]
