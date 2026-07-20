from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_core import memory


def _line(index: int, *, padding: int = 0) -> str:
    return json.dumps(
        {"id": index, "text": "한글" + ("x" * padding)},
        ensure_ascii=False,
        separators=(",", ":"),
    ) + "\n"


def test_jsonl_tail_does_not_read_entire_large_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    path = root / ".ai" / "memory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("".join(_line(i, padding=1000) for i in range(5000)), encoding="utf-8")
    monkeypatch.setattr(
        memory,
        "read_state_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("tail reader must not load the complete JSONL file")
        ),
    )

    rows = memory.read_jsonl_tail(path, 3)

    assert [row["id"] for row in rows] == [4997, 4998, 4999]


def test_jsonl_tail_discards_partial_utf8_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    path = root / ".ai" / "memory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("".join(_line(i, padding=30) for i in range(20)), encoding="utf-8")
    monkeypatch.setattr(memory, "_JSONL_TAIL_MIN_BYTES", 160)
    monkeypatch.setattr(memory, "_JSONL_TAIL_MAX_BYTES", 160)
    monkeypatch.setattr(memory, "_JSONL_TAIL_BYTES_PER_ITEM", 16)

    rows = memory.read_jsonl_tail(path, 2)

    assert [row["id"] for row in rows] == [18, 19]


def test_jsonl_tail_caps_requested_result_count(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    path = root / ".ai" / "memory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("".join(_line(i) for i in range(1100)), encoding="utf-8")

    rows = memory.read_jsonl_tail(path, 10_000_000)

    assert len(rows) == memory._JSONL_TAIL_MAX_LIMIT
    assert rows[0]["id"] == 100
    assert rows[-1]["id"] == 1099


def test_invalid_tail_limit_does_not_touch_filesystem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        memory,
        "read_root_confined_tail_bytes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid limit must stop before reading")
        ),
    )

    assert memory.read_jsonl_tail(tmp_path / "missing.jsonl", "bad") == []
    assert memory.read_jsonl_tail(tmp_path / "missing.jsonl", -1) == []


def test_single_oversized_trailing_record_fails_soft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    path = root / ".ai" / "memory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(_line(1, padding=10_000), encoding="utf-8")
    monkeypatch.setattr(memory, "_JSONL_TAIL_MIN_BYTES", 128)
    monkeypatch.setattr(memory, "_JSONL_TAIL_MAX_BYTES", 128)
    monkeypatch.setattr(memory, "_JSONL_TAIL_BYTES_PER_ITEM", 16)

    assert memory.read_jsonl_tail(path, 1) == []
