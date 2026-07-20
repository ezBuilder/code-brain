from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ai_core import audit_fold, memory, obs


def _audit_line(action: str) -> str:
    return json.dumps(
        {
            "ts": (datetime.now(timezone.utc) - timedelta(days=90)).isoformat().replace("+00:00", "Z"),
            "action": action,
            "category": "test",
            "payload": {"id": "x"},
        },
        separators=(",", ":"),
    ) + "\n"


@pytest.mark.skipif(os.name == "nt", reason="Unix parent symlink semantics")
def test_audit_discovery_never_traverses_memory_parent_symlink(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    external_memory = tmp_path / "external-memory"
    external_audit = external_memory / "audit"
    external_audit.mkdir(parents=True)
    outside = external_audit / "2026.jsonl"
    line = _audit_line("skill.recommend_pending")
    outside.write_text(line, encoding="utf-8")
    ai = root / ".ai"
    ai.mkdir(parents=True)
    (ai / "memory").symlink_to(external_memory, target_is_directory=True)

    assert memory.all_audit_files(root) == []
    summary = obs._surfacing_summary(root)
    folded = audit_fold.fold_old_entries(root, days=30)

    assert summary["surfaced_lifetime"] == 0
    assert folded["folded_days"] == 0
    assert outside.read_text(encoding="utf-8") == line


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_audit_discovery_omits_linked_file_and_observability_ignores_it(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    external = tmp_path / "external-audit.jsonl"
    line = _audit_line("skill.recommend_pending")
    external.write_text(line, encoding="utf-8")
    audit_dir = root / ".ai" / "memory" / "audit"
    audit_dir.mkdir(parents=True)
    (audit_dir / "2026.jsonl").symlink_to(external)

    assert memory.all_audit_files(root) == []
    assert obs._surfacing_summary(root)["surfaced_lifetime"] == 0
    assert external.read_text(encoding="utf-8") == line


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_audit_discovery_omits_hardlinked_file(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    external = tmp_path / "external-audit.jsonl"
    line = _audit_line("skill.recommend_pending")
    external.write_text(line, encoding="utf-8")
    audit_dir = root / ".ai" / "memory" / "audit"
    audit_dir.mkdir(parents=True)
    linked = audit_dir / "2026.jsonl"
    os.link(external, linked)

    assert memory.all_audit_files(root) == []
    assert audit_fold.fold_old_entries(root, days=30)["folded_days"] == 0
    assert external.read_text(encoding="utf-8") == line


def test_audit_discovery_accepts_only_canonical_year_files(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    audit_dir = root / ".ai" / "memory" / "audit"
    audit_dir.mkdir(parents=True)
    valid = audit_dir / "2025.jsonl"
    valid.write_text(_audit_line("skill.accept"), encoding="utf-8")
    (audit_dir / "25.jsonl").write_text(_audit_line("bad"), encoding="utf-8")
    (audit_dir / "2025.jsonl.bak").write_text(_audit_line("bad"), encoding="utf-8")
    (audit_dir / "abcd.jsonl").write_text(_audit_line("bad"), encoding="utf-8")

    assert memory.all_audit_files(root) == [valid]
    assert obs._surfacing_summary(root)["accepted"] == 1


def test_audit_fold_output_is_private(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    audit_dir = root / ".ai" / "memory" / "audit"
    audit_dir.mkdir(parents=True)
    path = audit_dir / "2025.jsonl"
    path.write_text(_audit_line("old.action"), encoding="utf-8")

    result = audit_fold.fold_old_entries(root, days=30)

    assert result["ok"] is True
    assert result["folded_days"] == 1
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert records == [
        {
            "action": "_folded",
            "payload": {
                "counts": {"old.action": 1},
                "date": records[0]["payload"]["date"],
                "source_files": [".ai/memory/audit/2025.jsonl"],
                "total": 1,
            },
            "ts": records[0]["ts"],
        }
    ]
