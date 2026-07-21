from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ai_core.doctor import check_audit_chain, check_audit_index
from ai_core import memory
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


def test_append_audit_compacts_automatically_and_repairs_index(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(memory, "AUDIT_MAX_BYTES", 2400)
    monkeypatch.setattr(memory, "AUDIT_KEEP_BYTES", 900)

    for index in range(40):
        append_audit(
            tmp_path,
            action="test.compact",
            category="test",
            payload={"index": index, "value": "x" * 120},
        )

    path = audit_path(tmp_path)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert path.stat().st_size <= memory.AUDIT_MAX_BYTES
    assert rows[-1]["payload"]["index"] == 39
    assert any(row.get("action") == "audit.retention_compact" for row in rows)
    assert check_audit_chain(tmp_path).ok is True
    assert check_audit_index(tmp_path).ok is True


def test_append_audit_prunes_expired_years_and_records_checkpoint(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(memory, "AUDIT_RETENTION_YEARS", 1)
    old_path = tmp_path / ".ai" / "memory" / "audit" / "2001.jsonl"
    old_path.parent.mkdir(parents=True)
    old_path.write_text('{"ts":"2001-01-01T00:00:00Z","action":"old","category":"test"}\n', encoding="utf-8")
    rebuild_audit_index(tmp_path)

    append_audit(tmp_path, action="test.current", category="test", payload={})

    assert not old_path.exists()
    current_rows = [
        json.loads(line)
        for line in audit_path(tmp_path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [row["action"] for row in current_rows][-2:] == ["audit.retention_prune", "test.current"]
    assert check_audit_chain(tmp_path).ok is True
    assert check_audit_index(tmp_path).ok is True


def test_append_audit_bounds_oversized_payload(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(memory, "AUDIT_PAYLOAD_MAX_BYTES", 300)
    monkeypatch.setattr(memory, "AUDIT_PAYLOAD_PREVIEW_BYTES", 80)

    record = append_audit(
        tmp_path,
        action="test.large-payload",
        category="test",
        payload={"blob": "z" * 10_000},
    )

    assert record["payload"]["truncated"] is True
    assert record["payload"]["original_bytes"] > 300
    assert len(record["payload"]["preview"].encode("utf-8")) <= 80


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