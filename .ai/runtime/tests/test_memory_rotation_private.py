from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ai_core import memory
from ai_core.memory import StateJsonlRecordLimit, append_jsonl, rotate_jsonl_tail
from ai_core.private_write import PrivateWriteSizeLimit


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_rotation_refuses_external_symlink_without_truncating_target(tmp_path: Path) -> None:
    external = tmp_path / "external.jsonl"
    external.write_text("".join(json.dumps({"id": index}) + "\n" for index in range(20)), encoding="utf-8")
    path = tmp_path / ".ai" / "memory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    path.symlink_to(external)
    original = external.read_text(encoding="utf-8")

    result = rotate_jsonl_tail(path, max_bytes=20, keep_lines=2)

    assert result["ok"] is False
    assert external.read_text(encoding="utf-8") == original


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_rotation_refuses_external_hardlink_without_truncating_target(tmp_path: Path) -> None:
    external = tmp_path / "external.jsonl"
    external.write_text("".join(json.dumps({"id": index}) + "\n" for index in range(20)), encoding="utf-8")
    path = tmp_path / ".ai" / "memory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    os.link(external, path)
    original = external.read_text(encoding="utf-8")

    result = rotate_jsonl_tail(path, max_bytes=20, keep_lines=2)

    assert result["ok"] is False
    assert external.read_text(encoding="utf-8") == original


def test_concurrent_append_and_rotation_preserve_all_new_records(tmp_path: Path) -> None:
    path = tmp_path / ".ai" / "memory" / "events.jsonl"
    for index in range(100):
        append_jsonl(path, {"id": f"old-{index}", "payload": "x" * 20})

    def append(index: int) -> None:
        append_jsonl(path, {"id": f"new-{index}", "payload": "y" * 20})

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(append, index) for index in range(30)]
        futures.extend(
            pool.submit(rotate_jsonl_tail, path, max_bytes=100_000, keep_lines=500)
            for _ in range(5)
        )
        for future in futures:
            future.result()

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    ids = {row["id"] for row in rows}
    assert {f"new-{index}" for index in range(30)}.issubset(ids)


def test_rotation_dry_run_leaves_file_unchanged(tmp_path: Path) -> None:
    path = tmp_path / ".ai" / "memory" / "events.jsonl"
    for index in range(20):
        append_jsonl(path, {"id": index, "payload": "x" * 20})
    original = path.read_bytes()

    result = rotate_jsonl_tail(path, max_bytes=50, keep_lines=2, dry_run=True)

    assert result["ok"] is True
    assert result["rotated"] is True
    assert path.read_bytes() == original


def test_rotation_does_not_use_whole_file_text_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / ".ai" / "memory" / "events.jsonl"
    for index in range(100):
        append_jsonl(path, {"id": index, "payload": "x" * 100})

    def unexpected_whole_read(*_args, **_kwargs):
        raise AssertionError("rotation must use bounded suffix reads")

    monkeypatch.setattr(memory, "read_root_confined_text", unexpected_whole_read)

    result = rotate_jsonl_tail(path, max_bytes=500, keep_lines=10)

    assert result["ok"] is True
    assert result["rotated"] is True
    assert path.stat().st_size <= 500


def test_rotation_reads_only_bounded_suffix_and_keeps_newest_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(memory, "JSONL_ROTATION_MIN_SCAN_BYTES", 128)
    monkeypatch.setattr(memory, "JSONL_ROTATION_MAX_SCAN_BYTES", 128)
    monkeypatch.setattr(memory, "JSONL_ROTATION_LINE_MAX_BYTES", 64)
    path = tmp_path / ".ai" / "memory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        ("x" * 10_000)
        + "\n"
        + json.dumps({"id": "latest-a"}, separators=(",", ":"))
        + "\n"
        + json.dumps({"id": "latest-b"}, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )

    result = rotate_jsonl_tail(path, max_bytes=64, keep_lines=2)

    assert result["ok"] is True
    assert result["scan_bytes"] <= 128
    assert result["scan_truncated"] is True
    assert result["lines_before"] == 3
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [row["id"] for row in rows] == ["latest-a", "latest-b"]
    assert path.stat().st_size <= 64


def test_rotation_rejects_newest_line_larger_than_hard_cap_without_mutation(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".ai" / "memory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"id": "old"})
        + "\n"
        + json.dumps({"id": "new", "payload": "x" * 100})
        + "\n",
        encoding="utf-8",
    )
    original = path.read_bytes()

    result = rotate_jsonl_tail(path, max_bytes=32, keep_lines=2)

    assert result["ok"] is False
    assert result["error"] == "newest JSONL line exceeds max_bytes"
    assert path.read_bytes() == original


def test_append_jsonl_file_cap_preserves_readable_existing_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / ".ai" / "memory" / "decisions.jsonl"
    first = {"id": "first"}
    first_bytes = len(
        (json.dumps(first, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    )
    monkeypatch.setattr(memory, "STATE_JSONL_MAX_BYTES", first_bytes)

    append_jsonl(path, first)
    original = path.read_bytes()
    with pytest.raises(PrivateWriteSizeLimit):
        append_jsonl(path, {"id": "second"})

    assert path.read_bytes() == original
    assert memory.read_jsonl_all(path) == [first]


def test_append_jsonl_line_cap_rejects_record_before_file_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / ".ai" / "memory" / "decisions.jsonl"
    monkeypatch.setattr(memory, "STATE_JSONL_MAX_LINE_BYTES", 32)

    with pytest.raises(PrivateWriteSizeLimit) as error:
        append_jsonl(path, {"payload": "x" * 100})

    assert error.value.maximum == 32
    assert not path.exists()


def test_concurrent_append_jsonl_never_crosses_global_state_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / ".ai" / "memory" / "events.jsonl"
    maximum = 1024
    monkeypatch.setattr(memory, "STATE_JSONL_MAX_BYTES", maximum)

    def append(index: int) -> bool:
        try:
            append_jsonl(path, {"id": index, "payload": "x" * 20})
            return True
        except PrivateWriteSizeLimit:
            return False

    with ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = list(pool.map(append, range(200)))

    assert any(outcomes)
    assert not all(outcomes)
    assert path.stat().st_size <= maximum
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == sum(outcomes)


def test_append_jsonl_record_cap_preserves_existing_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / ".ai" / "memory" / "decisions.jsonl"
    monkeypatch.setattr(memory, "STATE_JSONL_MAX_RECORDS", 2)
    append_jsonl(path, {"id": "first"})
    append_jsonl(path, {"id": "second"})
    original = path.read_bytes()

    with pytest.raises(StateJsonlRecordLimit) as error:
        append_jsonl(path, {"id": "third"})

    assert error.value.current == 3
    assert error.value.maximum == 2
    assert path.read_bytes() == original
    assert [row["id"] for row in memory.read_jsonl_all(path)] == ["first", "second"]


def test_append_jsonl_valid_count_sidecar_avoids_source_recount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / ".ai" / "memory" / "events.jsonl"
    append_jsonl(path, {"id": "first"})

    def unexpected_recount(*_args, **_kwargs):
        raise AssertionError("valid count metadata must avoid a source scan")

    monkeypatch.setattr(memory, "_scan_state_jsonl_records", unexpected_recount)

    append_jsonl(path, {"id": "second"})

    assert [row["id"] for row in memory.read_jsonl_all(path)] == ["first", "second"]


def test_append_jsonl_stale_count_sidecar_recounts_replaced_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / ".ai" / "memory" / "events.jsonl"
    append_jsonl(path, {"id": "old"})
    replacement = json.dumps({"id": "replacement"}, sort_keys=True, separators=(",", ":")) + "\n"
    memory.atomic_write_private_text(path, replacement, root=tmp_path)
    original_scan = memory._scan_state_jsonl_records
    scans = {"count": 0}

    def counted_scan(*args, **kwargs):
        scans["count"] += 1
        return original_scan(*args, **kwargs)

    monkeypatch.setattr(memory, "_scan_state_jsonl_records", counted_scan)
    append_jsonl(path, {"id": "new"})

    assert scans["count"] == 1
    assert [row["id"] for row in memory.read_jsonl_all(path)] == ["replacement", "new"]


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_append_jsonl_replaces_untrusted_count_sidecar_without_touching_target(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".ai" / "memory" / "events.jsonl"
    append_jsonl(path, {"id": "first"})
    sidecar = memory.jsonl_count_path(path)
    sidecar.unlink()
    external = tmp_path / "external-count.json"
    external.write_text('{"records":999999}\n', encoding="utf-8")
    sidecar.symlink_to(external)

    append_jsonl(path, {"id": "second"})

    assert external.read_text(encoding="utf-8") == '{"records":999999}\n'
    assert not sidecar.is_symlink()
    assert [row["id"] for row in memory.read_jsonl_all(path)] == ["first", "second"]


def test_concurrent_append_jsonl_never_crosses_record_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / ".ai" / "memory" / "events.jsonl"
    maximum = 25
    monkeypatch.setattr(memory, "STATE_JSONL_MAX_RECORDS", maximum)

    def append(index: int) -> bool:
        try:
            append_jsonl(path, {"id": index})
            return True
        except StateJsonlRecordLimit:
            return False

    with ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = list(pool.map(append, range(100)))

    assert sum(outcomes) == maximum
    rows = memory.read_jsonl_all(path)
    assert len(rows) == maximum