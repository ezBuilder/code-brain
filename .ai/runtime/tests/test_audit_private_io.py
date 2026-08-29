from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ai_core import memory
from ai_core.doctor import check_audit_chain, check_audit_index
from ai_core.memory import append_audit, audit_path, rebuild_audit_index


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_append_audit_refuses_external_symlink_without_touching_target(tmp_path: Path) -> None:
    path = audit_path(tmp_path, at=datetime.now(timezone.utc))
    path.parent.mkdir(parents=True)
    external = tmp_path / "external-audit.jsonl"
    external.write_text('{"external":true}\n', encoding="utf-8")
    path.symlink_to(external)

    with pytest.raises(OSError):
        append_audit(tmp_path, action="test.symlink", category="test", payload={})

    assert external.read_text(encoding="utf-8") == '{"external":true}\n'


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_append_audit_refuses_external_hardlink_without_touching_target(tmp_path: Path) -> None:
    path = audit_path(tmp_path, at=datetime.now(timezone.utc))
    path.parent.mkdir(parents=True)
    external = tmp_path / "external-audit.jsonl"
    external.write_text('{"external":true}\n', encoding="utf-8")
    os.link(external, path)

    with pytest.raises(OSError, match="hard links"):
        append_audit(tmp_path, action="test.hardlink", category="test", payload={})

    assert external.read_text(encoding="utf-8") == '{"external":true}\n'


@pytest.mark.skipif(os.name == "nt", reason="Unix directory symlink semantics")
def test_rebuild_audit_index_ignores_external_audit_directory_symlink(tmp_path: Path) -> None:
    external = tmp_path / "external-audit-dir"
    external.mkdir()
    (external / "2026.jsonl").write_text(
        '{"ts":"2026-01-01T00:00:00Z","action":"EXTERNAL","category":"test"}\n',
        encoding="utf-8",
    )
    audit_dir = tmp_path / ".ai" / "memory" / "audit"
    audit_dir.parent.mkdir(parents=True)
    audit_dir.symlink_to(external, target_is_directory=True)

    result = rebuild_audit_index(tmp_path)
    index = tmp_path / ".ai" / "memory" / "audit-index.jsonl"

    assert result["indexed"] == 0
    assert index.read_text(encoding="utf-8") == ""
    assert "EXTERNAL" in (external / "2026.jsonl").read_text(encoding="utf-8")


def test_concurrent_audit_append_and_index_rebuild_preserve_chain_and_rows(tmp_path: Path) -> None:
    def append(index: int) -> None:
        append_audit(
            tmp_path,
            action="test.concurrent",
            category="test",
            payload={"index": index},
        )

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(append, index) for index in range(40)]
        futures.extend(pool.submit(rebuild_audit_index, tmp_path) for _ in range(5))
        for future in futures:
            future.result()

    result = rebuild_audit_index(tmp_path)
    rows = [
        json.loads(line)
        for line in (tmp_path / ".ai" / "memory" / "audit-index.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]

    assert result["indexed"] == 40
    assert len(rows) == 40
    assert check_audit_chain(tmp_path).ok is True


def test_bounded_audit_index_validates_retained_tail(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(memory, "_AUDIT_INDEX_MAX_ROWS", 2)
    for index in range(3):
        append_audit(
            tmp_path,
            action=f"test.{index}",
            category="test",
            payload={"index": index},
        )

    result = rebuild_audit_index(tmp_path)

    assert result["indexed"] == 2
    assert check_audit_index(tmp_path).ok is True


def test_new_audit_records_have_unique_stable_event_ids(tmp_path: Path) -> None:
    records = [
        append_audit(tmp_path, action=f"test.{index}", category="test", payload={"index": index})
        for index in range(3)
    ]
    event_ids = [record["event_id"] for record in records]
    assert len(set(event_ids)) == 3
    assert all(event_id.startswith("evt-") and len(event_id) == 36 for event_id in event_ids)
    persisted = [
        json.loads(line)["event_id"]
        for line in audit_path(tmp_path, at=datetime.now(timezone.utc)).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert persisted == event_ids


def test_audit_segmentation_preserves_every_event_id_without_loss(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(memory, "_AUDIT_MAX_BYTES", 2_000)
    monkeypatch.setattr(memory, "_AUDIT_LINE_MAX_BYTES", 400)

    written = [
        append_audit(tmp_path, action="test.rotation", category="test", payload={"index": index, "v": "x" * 80})
        for index in range(40)
    ]
    persisted = [
        json.loads(line)
        for path in memory.all_audit_files(tmp_path)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    written_ids = {record["event_id"] for record in written}
    retained_ids = {
        record["event_id"]
        for record in persisted
        if record.get("action") == "test.rotation"
    }
    markers = [record for record in persisted if record.get("action") == "audit.segment_started"]
    assert retained_ids == written_ids
    assert markers and all(marker["payload"]["lossy"] is False for marker in markers)
    assert all("bytes_discarded" not in marker["payload"] for marker in markers)
    assert check_audit_chain(tmp_path).ok is True


def test_audit_segment_link_tamper_is_detected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(memory, "_AUDIT_MAX_BYTES", 2_000)
    monkeypatch.setattr(memory, "_AUDIT_LINE_MAX_BYTES", 400)
    for index in range(30):
        append_audit(
            tmp_path,
            action="test.segment",
            category="test",
            payload={"index": index, "v": "x" * 80},
        )

    current = audit_path(tmp_path, at=datetime.now(timezone.utc))
    rows = [json.loads(line) for line in current.read_text(encoding="utf-8").splitlines()]
    rows[0]["payload"]["previous_last_sha"] = "0" * 64
    current.write_text(
        "\n".join(json.dumps(row, sort_keys=True, separators=(",", ":")) for row in rows) + "\n",
        encoding="utf-8",
    )

    result = check_audit_chain(tmp_path)
    assert result.ok is False
    assert "segment_link_line_mismatch" in result.detail


@pytest.mark.parametrize("missing_position", [0, 1], ids=["first", "middle"])
def test_doctor_detects_deleted_raw_segment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_position: int
) -> None:
    monkeypatch.setattr(memory, "_AUDIT_MAX_BYTES", 2_000)
    monkeypatch.setattr(memory, "_AUDIT_LINE_MAX_BYTES", 400)
    for index in range(60):
        append_audit(
            tmp_path,
            action="test.segment.loss",
            category="test",
            payload={"index": index, "v": "x" * 80},
        )
    segments = [
        path
        for path in memory.all_audit_files(tmp_path)
        if (memory._audit_file_sort_key(path.name) or (0, 1, 0))[1] == 0
    ]
    assert len(segments) >= 3

    segments[missing_position].unlink()
    result = check_audit_chain(tmp_path)

    assert result.ok is False
    expected = "segment_sequence_start" if missing_position == 0 else "segment_sequence_gap"
    assert expected in result.detail


def test_audit_segmentation_reuses_crash_left_segment(tmp_path: Path, monkeypatch) -> None:
    for index in range(12):
        append_audit(tmp_path, action="test.seed", category="test", payload={"index": index})
    current = audit_path(tmp_path, at=datetime.now(timezone.utc))
    raw = current.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    segment = current.with_name(f"{current.name[:4]}.000001.{digest[:12]}.jsonl")
    segment.write_bytes(raw)
    segment.chmod(0o600)

    monkeypatch.setattr(memory, "_AUDIT_MAX_BYTES", len(raw))
    append_audit(tmp_path, action="test.after_crash", category="test", payload={})

    files = memory.all_audit_files(tmp_path)
    assert files == [segment, current]
    actions = [
        json.loads(line)["action"]
        for path in files
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert actions.count("test.seed") == 12
    assert actions.count("test.after_crash") == 1
    assert check_audit_chain(tmp_path).ok is True


def test_duplicate_audit_segment_sequence_is_deterministic_and_rejected(tmp_path: Path) -> None:
    audit_dir = tmp_path / ".ai" / "memory" / "audit"
    audit_dir.mkdir(parents=True)
    contents = ['{"action":"branch-a"}\n', '{"action":"branch-b"}\n']
    segments = []
    for content in contents:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
        segment = audit_dir / f"2026.000001.{digest}.jsonl"
        segment.write_text(content, encoding="utf-8")
        segments.append(segment)

    discovered = memory.all_audit_files(tmp_path)

    assert discovered == sorted(segments, key=lambda path: path.name)
    result = check_audit_chain(tmp_path)
    assert result.ok is False
    assert "segment_sequence_duplicate" in result.detail


def test_audit_chain_rejects_duplicate_event_ids(tmp_path: Path) -> None:
    append_audit(tmp_path, action="test.one", category="test", payload={})
    append_audit(tmp_path, action="test.two", category="test", payload={})
    path = audit_path(tmp_path, at=datetime.now(timezone.utc))
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[1]["event_id"] = rows[0]["event_id"]
    path.write_text("\n".join(json.dumps(row, sort_keys=True, separators=(",", ":")) for row in rows) + "\n", encoding="utf-8")

    result = check_audit_chain(tmp_path)

    assert result.ok is False
    assert "event_id_duplicate" in result.detail


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_strict_audit_checks_reject_external_directory_symlink(tmp_path: Path) -> None:
    external = tmp_path / "external-audit"
    external.mkdir()
    (external / "2026.jsonl").write_text('{"action":"EXTERNAL"}\n', encoding="utf-8")
    audit_dir = tmp_path / ".ai" / "memory" / "audit"
    audit_dir.parent.mkdir(parents=True)
    audit_dir.symlink_to(external, target_is_directory=True)

    assert check_audit_chain(tmp_path).ok is False
    assert check_audit_index(tmp_path).ok is False
    assert "EXTERNAL" in (external / "2026.jsonl").read_text(encoding="utf-8")


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_strict_audit_checks_reject_external_hardlinked_file(tmp_path: Path) -> None:
    external = tmp_path / "external-audit.jsonl"
    external.write_text('{"ts":"2026-01-01T00:00:00Z","action":"EXTERNAL","category":"x"}\n', encoding="utf-8")
    audit_file = tmp_path / ".ai" / "memory" / "audit" / "2026.jsonl"
    audit_file.parent.mkdir(parents=True)
    os.link(external, audit_file)

    assert check_audit_chain(tmp_path).ok is False
    assert check_audit_index(tmp_path).ok is False
    assert "EXTERNAL" in external.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_strict_audit_index_rejects_external_index_symlink(tmp_path: Path) -> None:
    external = tmp_path / "external-index.jsonl"
    external.write_text('{"path":".ai/memory/audit/2026.jsonl"}\n', encoding="utf-8")
    index = tmp_path / ".ai" / "memory" / "audit-index.jsonl"
    index.parent.mkdir(parents=True)
    index.symlink_to(external)

    check = check_audit_index(tmp_path)

    assert check.ok is False
    assert "audit-index-untrusted" in check.detail
    assert external.read_text(encoding="utf-8").startswith('{"path"')
