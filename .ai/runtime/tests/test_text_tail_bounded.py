from __future__ import annotations

import os
from pathlib import Path

import pytest

from ai_core import memory


def test_text_tail_does_not_read_complete_large_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    path = root / ".ai" / "memory" / "notes.md"
    path.parent.mkdir(parents=True)
    path.write_text("".join(f"line-{index}-" + ("x" * 1000) + "\n" for index in range(5000)), encoding="utf-8")
    monkeypatch.setattr(
        memory,
        "read_state_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("text tail must not load the complete file")
        ),
    )

    tail = memory.read_text_tail(path, 3)

    assert [line.split("-", 2)[:2] for line in tail.splitlines()] == [
        ["line", "4997"],
        ["line", "4998"],
        ["line", "4999"],
    ]


def test_text_tail_discards_partial_utf8_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    path = root / ".ai" / "memory" / "notes.md"
    path.parent.mkdir(parents=True)
    path.write_text("".join(f"기록-{index}-" + ("가" * 20) + "\n" for index in range(20)), encoding="utf-8")
    monkeypatch.setattr(memory, "_TEXT_TAIL_MIN_BYTES", 180)
    monkeypatch.setattr(memory, "_TEXT_TAIL_MAX_BYTES", 180)
    monkeypatch.setattr(memory, "_TEXT_TAIL_BYTES_PER_LINE", 16)

    tail = memory.read_text_tail(path, 2)

    assert [line.split("-", 2)[:2] for line in tail.splitlines()] == [
        ["기록", "18"],
        ["기록", "19"],
    ]


def test_text_tail_caps_requested_line_count(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    path = root / ".ai" / "memory" / "notes.md"
    path.parent.mkdir(parents=True)
    path.write_text("".join(f"line-{index}\n" for index in range(1100)), encoding="utf-8")

    tail = memory.read_text_tail(path, 10**100)
    lines = tail.splitlines()

    assert len(lines) == memory._TEXT_TAIL_MAX_LINES
    assert lines[0] == "line-100"
    assert lines[-1] == "line-1099"


def test_invalid_text_tail_limit_stops_before_filesystem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        memory,
        "read_root_confined_tail_bytes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid line count must not read")
        ),
    )

    assert memory.read_text_tail(tmp_path / "missing.md", "bad") == ""
    assert memory.read_text_tail(tmp_path / "missing.md", -1) == ""


def test_single_oversized_trailing_line_fails_soft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    path = root / ".ai" / "memory" / "notes.md"
    path.parent.mkdir(parents=True)
    path.write_text("가" * 10_000, encoding="utf-8")
    monkeypatch.setattr(memory, "_TEXT_TAIL_MIN_BYTES", 128)
    monkeypatch.setattr(memory, "_TEXT_TAIL_MAX_BYTES", 128)
    monkeypatch.setattr(memory, "_TEXT_TAIL_BYTES_PER_LINE", 16)

    assert memory.read_text_tail(path, 1) == ""


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_text_tail_rejects_linked_file_without_external_read(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    external = tmp_path / "outside.md"
    external.write_text("outside secret\n", encoding="utf-8")
    path = root / ".ai" / "memory" / "notes.md"
    path.parent.mkdir(parents=True)
    path.symlink_to(external)

    assert memory.read_text_tail(path, 2) == ""
    assert external.read_text(encoding="utf-8") == "outside secret\n"


def test_text_tail_preserves_existing_rstrip_contract(tmp_path: Path) -> None:
    path = tmp_path / "repo" / ".ai" / "memory" / "notes.md"
    path.parent.mkdir(parents=True)
    path.write_text("one\ntwo  \nthree\n\n  \n", encoding="utf-8")

    assert memory.read_text_tail(path, 2) == "two  \nthree"
