from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ai_core import memory


def _line(index: int, *, padding: int = 0) -> str:
    return json.dumps(
        {"id": index, "text": "한글" + ("x" * padding)},
        ensure_ascii=False,
        separators=(",", ":"),
    ) + "\n"


def test_rotation_does_not_read_complete_large_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    path = root / ".ai" / "memory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("".join(_line(i, padding=1000) for i in range(5000)), encoding="utf-8")
    monkeypatch.setattr(
        memory,
        "read_root_confined_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("rotation must not load the complete JSONL file")
        ),
    )

    result = memory.rotate_jsonl_tail(path, max_bytes=4096, keep_lines=50)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    assert result["ok"] is True
    assert result["rotated"] is True
    assert path.stat().st_size <= 4096
    assert rows[-1]["id"] == 4999
    assert [row["id"] for row in rows] == sorted(row["id"] for row in rows)


def test_rotation_discards_partial_utf8_prefix(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    path = root / ".ai" / "memory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("".join(_line(i, padding=30) for i in range(100)), encoding="utf-8")

    result = memory.rotate_jsonl_tail(path, max_bytes=220, keep_lines=5)
    text = path.read_bytes().decode("utf-8")
    rows = [json.loads(line) for line in text.splitlines()]

    assert result["ok"] is True
    assert path.stat().st_size <= 220
    assert rows[-1]["id"] == 99


def test_rotation_drops_individual_line_that_cannot_fit(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    path = root / ".ai" / "memory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        _line(1, padding=10) + _line(2, padding=2000),
        encoding="utf-8",
    )

    result = memory.rotate_jsonl_tail(path, max_bytes=128, keep_lines=10)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    assert result["ok"] is True
    assert path.stat().st_size <= 128
    assert [row["id"] for row in rows] == [1]


@pytest.mark.skipif(os.name == "nt", reason="Unix directory symlink semantics")
def test_invalid_rotation_bounds_do_not_follow_external_parent(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    external = tmp_path / "external-memory"
    external.mkdir()
    outside = external / "events.jsonl"
    outside.write_text(_line(1), encoding="utf-8")
    ai = root / ".ai"
    ai.mkdir(parents=True)
    (ai / "memory").symlink_to(external, target_is_directory=True)
    original = outside.read_bytes()

    result = memory.rotate_jsonl_tail(
        root / ".ai" / "memory" / "events.jsonl",
        max_bytes="bad",
        keep_lines=10,
    )

    assert result["ok"] is False
    assert result["error"] == "invalid_bounds"
    assert outside.read_bytes() == original


def test_rotation_caps_unbounded_requested_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    path = root / ".ai" / "memory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("".join(_line(i, padding=20) for i in range(50)), encoding="utf-8")
    monkeypatch.setattr(memory, "_JSONL_ROTATE_MAX_BYTES", 256)
    monkeypatch.setattr(memory, "_JSONL_ROTATE_MAX_LINES", 3)

    result = memory.rotate_jsonl_tail(
        path,
        max_bytes=10**100,
        keep_lines=10**100,
    )
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    assert result["ok"] is True
    assert path.stat().st_size <= 256
    assert len(rows) <= 3
    assert rows[-1]["id"] == 49


def test_rotation_zero_budget_produces_empty_file(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    path = root / ".ai" / "memory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(_line(1), encoding="utf-8")

    result = memory.rotate_jsonl_tail(path, max_bytes=0, keep_lines=10)

    assert result["ok"] is True
    assert result["rotated"] is True
    assert path.read_bytes() == b""
