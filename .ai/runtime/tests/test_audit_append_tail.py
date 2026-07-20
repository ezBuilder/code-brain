from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ai_core import memory
from ai_core.private_write import read_root_confined_tail_bytes


def test_append_audit_hashes_last_line_without_full_file_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    path = memory.audit_path(root)
    path.parent.mkdir(parents=True)
    previous = json.dumps({"action": "previous", "payload": {"value": 1}}, separators=(",", ":"))
    prefix = (("x" * 1000) + "\n") * 2500
    path.write_text(prefix + previous + "\n", encoding="utf-8")

    monkeypatch.setattr(
        memory,
        "read_root_confined_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("append hot path must not read the full audit file")
        ),
    )

    record = memory.append_audit(
        root,
        action="current.action",
        category="test",
        payload={"value": 2},
    )

    assert record["prev_sha"] == memory.line_sha(previous)
    final_line = path.read_text(encoding="utf-8").splitlines()[-1]
    assert json.loads(final_line)["action"] == "current.action"


def test_oversized_audit_payload_is_replaced_by_bounded_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    monkeypatch.setattr(memory, "_AUDIT_LINE_MAX_BYTES", 1024)
    payload = {"blob": "x" * 20_000}

    record = memory.append_audit(
        root,
        action="large.payload",
        category="test",
        payload=payload,
    )

    expected_payload = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert record["payload"] == {
        "_truncated": True,
        "bytes": len(expected_payload),
        "sha256": hashlib.sha256(expected_payload).hexdigest(),
    }
    line = memory.audit_path(root).read_bytes().splitlines()[-1]
    assert len(line) <= 1024


def test_oversized_existing_last_record_fails_without_modification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    monkeypatch.setattr(memory, "_AUDIT_LINE_MAX_BYTES", 1024)
    path = memory.audit_path(root)
    path.parent.mkdir(parents=True)
    original = ("x" * 3000) + "\n"
    path.write_text(original, encoding="utf-8")

    with pytest.raises(OSError, match="exceeds line limit"):
        memory.append_audit(root, action="new", category="test", payload={})

    assert path.read_text(encoding="utf-8") == original


def test_audit_action_and_category_are_capped_consistently(tmp_path: Path) -> None:
    root = tmp_path / "repo"

    record = memory.append_audit(
        root,
        action="a" * (memory._AUDIT_ACTION_MAX_CHARS + 50),
        category="c" * (memory._AUDIT_CATEGORY_MAX_CHARS + 50),
        payload={},
    )

    assert len(record["action"]) == memory._AUDIT_ACTION_MAX_CHARS
    assert len(record["category"]) == memory._AUDIT_CATEGORY_MAX_CHARS
    index_record = json.loads(
        (root / ".ai" / "memory" / "audit-index.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[-1]
    )
    assert index_record["action"] == record["action"]
    assert index_record["category"] == record["category"]


def test_confined_tail_reader_reports_complete_and_suffix_reads(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    path = root / ".ai" / "memory" / "sample.log"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"0123456789")

    full, full_state, full_complete = read_root_confined_tail_bytes(
        path,
        root=root,
        max_bytes=20,
        require_private=False,
    )
    tail, tail_state, tail_complete = read_root_confined_tail_bytes(
        path,
        root=root,
        max_bytes=4,
        require_private=False,
    )

    assert full == b"0123456789"
    assert full_complete is True
    assert tail == b"6789"
    assert tail_complete is False
    assert full_state.st_ino == tail_state.st_ino
