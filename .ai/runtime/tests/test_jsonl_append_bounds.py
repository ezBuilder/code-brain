from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ai_core import memory


def test_oversized_jsonl_record_is_rejected_before_file_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    path = root / ".ai" / "memory" / "records.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text('{"seed":true}\n', encoding="utf-8")
    original = path.read_bytes()
    monkeypatch.setattr(memory, "_JSONL_LINE_MAX_BYTES", 128)

    with pytest.raises(OSError, match="exceeds line limit"):
        memory.append_jsonl(path, {"value": "x" * 1000})

    assert path.read_bytes() == original


def test_jsonl_byte_limit_counts_utf8_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "repo" / ".ai" / "memory" / "records.jsonl"
    monkeypatch.setattr(memory, "_JSONL_LINE_MAX_BYTES", 80)

    with pytest.raises(OSError, match="exceeds line limit"):
        memory.append_jsonl(path, {"value": "가" * 40})

    assert not path.exists()


def test_non_finite_jsonl_numbers_are_rejected_without_writing(tmp_path: Path) -> None:
    path = tmp_path / "repo" / ".ai" / "memory" / "records.jsonl"

    with pytest.raises(ValueError, match="Out of range float values"):
        memory.append_jsonl(path, {"value": float("nan")})

    assert not path.exists()


def test_normal_jsonl_record_remains_canonical_and_private(tmp_path: Path) -> None:
    path = tmp_path / "repo" / ".ai" / "memory" / "records.jsonl"
    record = {"z": 1, "a": "한글"}

    memory.append_jsonl(path, record)

    line = path.read_text(encoding="utf-8").strip()
    assert line == json.dumps(
        record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0


def test_large_event_still_fits_generic_jsonl_limit(tmp_path: Path) -> None:
    root = tmp_path / "repo"

    record = memory.append_event(
        root,
        {
            "hook": "UserPromptSubmit",
            "agent": "codex",
            "payload": "x" * (memory.EVENT_PAYLOAD_MAX_BYTES * 10),
        },
    )

    assert record["payload"]["truncated"] is True
    event_line = memory.events_path(root).read_bytes().splitlines()[-1]
    assert len(event_line) <= memory._JSONL_LINE_MAX_BYTES
