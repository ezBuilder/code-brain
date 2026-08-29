"""
Tests for audit_fold module.

Covers:
- Empty audit directory
- No folding when all entries are recent
- Mixed recent and old entries
- Idempotence (already-folded entries stay untouched)
- dry_run doesn't modify files
- Malformed JSON is preserved
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from ai_core.audit_fold import fold_old_entries


@pytest.fixture
def audit_root(tmp_path: Path) -> Path:
    """Create a temporary root with .ai/memory/audit directory."""
    audit_dir = tmp_path / ".ai" / "memory" / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    return tmp_path


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _past_iso(days: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.isoformat().replace("+00:00", "Z")


def _make_audit_entry(action: str, ts: str) -> str:
    record = {
        "ts": ts,
        "action": action,
        "category": "test",
        "payload": {"test": True},
    }
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class TestEmptyAudit:
    """Test behavior with no audit files."""

    def test_empty_directory(self, audit_root: Path) -> None:
        result = fold_old_entries(audit_root, days=30)
        assert result["ok"] is True
        assert result["folded_days"] == 0
        assert result["removed_entries"] == 0
        assert result["added_fold_records"] == 0


class TestNoFolding:
    """Test when all entries are recent (within cutoff)."""

    def test_all_recent_entries(self, audit_root: Path) -> None:
        audit_file = audit_root / ".ai" / "memory" / "audit" / "2026.jsonl"
        audit_file.parent.mkdir(parents=True, exist_ok=True)

        lines = [
            _make_audit_entry("action.a", _now_iso()),
            _make_audit_entry("action.b", _now_iso()),
        ]
        audit_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = fold_old_entries(audit_root, days=30)
        assert result["ok"] is True
        assert result["folded_days"] == 0
        assert result["removed_entries"] == 0
        assert result["added_fold_records"] == 0

        # File unchanged
        content = audit_file.read_text(encoding="utf-8")
        assert len(content.splitlines()) == 2


class TestMixedAge:
    """Test with mixed recent and old entries."""

    def test_fold_old_keep_recent(self, audit_root: Path) -> None:
        audit_file = audit_root / ".ai" / "memory" / "audit" / "2026.jsonl"
        audit_file.parent.mkdir(parents=True, exist_ok=True)

        lines = [
            _make_audit_entry("old.action", _past_iso(40)),
            _make_audit_entry("old.action", _past_iso(35)),
            _make_audit_entry("recent.action", _now_iso()),
        ]
        audit_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        original = audit_file.read_bytes()

        result = fold_old_entries(audit_root, days=30)
        assert result["ok"] is True
        assert result["folded_days"] >= 1  # At least one date folded
        assert result["removed_entries"] == 0
        assert result["source_entries"] == 2
        assert result["rolled_up_entries"] == 2
        assert result["added_fold_records"] >= 1

        # Raw audit remains byte-identical; the derived record is private.
        assert audit_file.read_bytes() == original
        sidecar = audit_root / ".ai" / "memory" / "audit-rollups" / "2026.jsonl"
        rollups = [json.loads(line) for line in sidecar.read_text(encoding="utf-8").splitlines()]
        assert len(rollups) >= 1
        assert rollups[0]["kind"] == "audit_rollup"
        assert rollups[0]["source"]["path"] == ".ai/memory/audit/2026.jsonl"


class TestIdempotence:
    """Test that already-folded entries aren't re-folded."""

    def test_already_folded_untouched(self, audit_root: Path) -> None:
        audit_file = audit_root / ".ai" / "memory" / "audit" / "2026.jsonl"
        audit_file.parent.mkdir(parents=True, exist_ok=True)

        # Create a pre-existing fold record and a recent entry
        fold_record = {
            "ts": "2026-04-20T23:59:59Z",
            "action": "_folded",
            "payload": {
                "date": "2026-04-20",
                "counts": {"old.action": 5},
                "total": 5,
                "source_files": ["audit/2026.jsonl"],
            },
        }
        recent = _make_audit_entry("recent.action", _now_iso())

        lines = [
            json.dumps(fold_record, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            recent,
        ]
        audit_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = fold_old_entries(audit_root, days=30)
        assert result["ok"] is True
        assert result["folded_days"] == 0  # No new folding
        assert result["removed_entries"] == 0

        # File unchanged
        content = audit_file.read_text(encoding="utf-8")
        assert len(content.splitlines()) == 2


class TestDryRun:
    """Test that dry_run doesn't modify files."""

    def test_dry_run_no_modification(self, audit_root: Path) -> None:
        audit_file = audit_root / ".ai" / "memory" / "audit" / "2026.jsonl"
        audit_file.parent.mkdir(parents=True, exist_ok=True)

        lines = [
            _make_audit_entry("old.action", _past_iso(40)),
            _make_audit_entry("recent.action", _now_iso()),
        ]
        original = "\n".join(lines) + "\n"
        audit_file.write_text(original, encoding="utf-8")

        result = fold_old_entries(audit_root, days=30, dry_run=True)
        assert result["ok"] is True
        assert result["dry_run"] is True
        assert result["folded_days"] >= 1  # Would fold
        assert result["removed_entries"] == 0
        assert result["source_entries"] == 1
        assert not (audit_root / ".ai" / "memory" / "audit-rollups").exists()

        # File unchanged
        content = audit_file.read_text(encoding="utf-8")
        assert content == original


class TestMalformedJSON:
    """Test that malformed JSON lines are preserved."""

    def test_preserve_malformed(self, audit_root: Path) -> None:
        audit_file = audit_root / ".ai" / "memory" / "audit" / "2026.jsonl"
        audit_file.parent.mkdir(parents=True, exist_ok=True)

        valid = _make_audit_entry("action.a", _past_iso(40))
        malformed = "this is not valid json at all {][}"
        recent = _make_audit_entry("action.b", _now_iso())

        lines = [valid, malformed, recent]
        audit_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = fold_old_entries(audit_root, days=30)
        assert result["ok"] is True

        # Verify malformed line is still there
        content = audit_file.read_text(encoding="utf-8")
        assert malformed in content


class TestMultipleFiles:
    """Test folding across multiple audit files."""

    def test_fold_multiple_years(self, audit_root: Path) -> None:
        audit_dir = audit_root / ".ai" / "memory" / "audit"

        # 2025.jsonl with old entries
        file_2025 = audit_dir / "2025.jsonl"
        file_2025.write_text(
            _make_audit_entry("old.action", _past_iso(365)) + "\n",
            encoding="utf-8",
        )

        # 2026.jsonl with mixed
        file_2026 = audit_dir / "2026.jsonl"
        lines = [
            _make_audit_entry("old.action", _past_iso(40)),
            _make_audit_entry("recent.action", _now_iso()),
        ]
        file_2026.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = fold_old_entries(audit_root, days=30)
        assert result["ok"] is True
        assert result["removed_entries"] == 0
        assert result["source_entries"] >= 2
        assert len(result["files_touched"]) >= 1


class TestFoldStructure:
    """Test the structure of generated fold records."""

    def test_fold_record_format(self, audit_root: Path) -> None:
        audit_file = audit_root / ".ai" / "memory" / "audit" / "2026.jsonl"
        audit_file.parent.mkdir(parents=True, exist_ok=True)

        ts_35d = _past_iso(35)
        ts_32d = _past_iso(32)

        lines = [
            _make_audit_entry("action.x", ts_35d),
            _make_audit_entry("action.y", ts_35d),
            _make_audit_entry("action.x", ts_32d),
        ]
        audit_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = fold_old_entries(audit_root, days=30)
        assert result["ok"] is True
        assert result["folded_days"] == 2  # Two separate dates
        assert result["removed_entries"] == 0
        assert result["source_entries"] == 3

        sidecar = audit_root / ".ai" / "memory" / "audit-rollups" / "2026.jsonl"
        fold_recs = [json.loads(l) for l in sidecar.read_text(encoding="utf-8").splitlines()]
        assert len(fold_recs) == 2

        for fold in fold_recs:
            assert fold["kind"] == "audit_rollup"
            assert "rollup_id" in fold
            assert "source" in fold
            payload = fold.get("payload", {})
            assert "date" in payload
            assert "counts" in payload
            assert "total" in payload
            assert "source_files" in payload
            assert payload["total"] > 0


class TestEmptyLines:
    """Test handling of blank lines in audit file."""

    def test_empty_lines_ignored(self, audit_root: Path) -> None:
        audit_file = audit_root / ".ai" / "memory" / "audit" / "2026.jsonl"
        audit_file.parent.mkdir(parents=True, exist_ok=True)

        content = (
            _make_audit_entry("action.a", _past_iso(40))
            + "\n\n"  # blank line
            + _make_audit_entry("action.b", _now_iso())
            + "\n"
        )
        audit_file.write_text(content, encoding="utf-8")
        original = audit_file.read_bytes()

        result = fold_old_entries(audit_root, days=30)
        assert result["ok"] is True
        assert result["removed_entries"] == 0
        assert result["source_entries"] == 1

        # Raw audit and its blank line remain untouched.
        assert audit_file.read_bytes() == original
        sidecar = audit_root / ".ai" / "memory" / "audit-rollups" / "2026.jsonl"
        assert len(sidecar.read_text(encoding="utf-8").splitlines()) == 1


class TestDisabledFolding:
    """Test that days=0 disables folding."""

    def test_days_zero_no_fold(self, audit_root: Path) -> None:
        audit_file = audit_root / ".ai" / "memory" / "audit" / "2026.jsonl"
        audit_file.parent.mkdir(parents=True, exist_ok=True)

        lines = [_make_audit_entry("old.action", _past_iso(40))]
        original = "\n".join(lines) + "\n"
        audit_file.write_text(original, encoding="utf-8")

        result = fold_old_entries(audit_root, days=0)
        assert result["ok"] is True
        assert result["folded_days"] == 0

        # File unchanged
        assert audit_file.read_text(encoding="utf-8") == original


def test_fold_is_idempotent_and_never_rewrites_raw_audit(audit_root: Path) -> None:
    audit_file = audit_root / ".ai" / "memory" / "audit" / "2026.jsonl"
    audit_file.parent.mkdir(parents=True, exist_ok=True)
    audit_file.write_text(_make_audit_entry("old.action", _past_iso(40)) + "\n", encoding="utf-8")

    first = fold_old_entries(audit_root, days=30)
    raw_after_first = audit_file.read_bytes()
    sidecar = audit_root / ".ai" / "memory" / "audit-rollups" / "2026.jsonl"
    sidecar_after_first = sidecar.read_bytes()
    second = fold_old_entries(audit_root, days=30)

    assert first["added_fold_records"] == 1
    assert second["added_fold_records"] == 0
    assert second["folded_days"] == 0
    assert audit_file.read_bytes() == raw_after_first
    assert sidecar.read_bytes() == sidecar_after_first


def test_rollup_is_deterministic_and_backdated_append_replaces_date_record(audit_root: Path) -> None:
    audit_file = audit_root / ".ai" / "memory" / "audit" / "2026.jsonl"
    audit_file.parent.mkdir(parents=True, exist_ok=True)
    first = {"ts": _past_iso(40), "action": "old.one", "category": "test", "payload": {}, "event_id": "evt-" + "1" * 32}
    audit_file.write_text(json.dumps(first, sort_keys=True) + "\n", encoding="utf-8")

    fold_old_entries(audit_root, days=30)
    sidecar = audit_root / ".ai" / "memory" / "audit-rollups" / "2026.jsonl"
    first_bytes = sidecar.read_bytes()
    sidecar.unlink()
    fold_old_entries(audit_root, days=30)
    assert sidecar.read_bytes() == first_bytes

    second = {"ts": _past_iso(40), "action": "old.two", "category": "test", "payload": {}, "event_id": "evt-" + "2" * 32}
    with audit_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(second, sort_keys=True) + "\n")
    result = fold_old_entries(audit_root, days=30)
    records = [json.loads(line) for line in sidecar.read_text(encoding="utf-8").splitlines()]

    assert result["added_fold_records"] == 0
    assert result["updated_fold_records"] == 1
    assert len(records) == 1
    assert records[0]["payload"]["total"] == 2
    assert records[0]["payload"]["counts"] == {"old.one": 1, "old.two": 1}
    assert records[0]["ts"] == records[0]["source"]["date"] + "T23:59:59Z"


def test_large_day_uses_bounded_event_anchors(audit_root: Path) -> None:
    audit_file = audit_root / ".ai" / "memory" / "audit" / "2026.jsonl"
    audit_file.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "ts": _past_iso(40),
            "action": "bulk.action",
            "category": "test",
            "payload": {},
            "event_id": f"evt-{index:032x}",
        }
        for index in range(1_000)
    ]
    audit_file.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")

    fold_old_entries(audit_root, days=30)
    sidecar = audit_root / ".ai" / "memory" / "audit-rollups" / "2026.jsonl"
    source = json.loads(sidecar.read_text(encoding="utf-8"))["source"]

    assert "event_ids" not in source
    assert len(source["event_id_anchors"]) <= 32
    assert source["event_id_first"] == rows[0]["event_id"]
    assert source["event_id_last"] == rows[-1]["event_id"]
    assert source["event_id_count"] == 1_000
    assert source["line_ranges"] == [[1, 1_000]]


def test_legacy_fold_rows_remain_read_compatible_without_raw_rewrite(audit_root: Path) -> None:
    audit_file = audit_root / ".ai" / "memory" / "audit" / "2026.jsonl"
    audit_file.parent.mkdir(parents=True, exist_ok=True)
    legacy_fold = {
        "ts": "2026-01-01T23:59:59Z",
        "action": "_folded",
        "payload": {"date": "2026-01-01", "counts": {"legacy": 2}, "total": 2},
    }
    raw = json.dumps(legacy_fold, sort_keys=True) + "\n" + _make_audit_entry("new.old", _past_iso(40)) + "\n"
    audit_file.write_text(raw, encoding="utf-8")

    result = fold_old_entries(audit_root, days=30)

    assert result["ok"] is True
    assert audit_file.read_text(encoding="utf-8") == raw
    rollups = (audit_root / ".ai" / "memory" / "audit-rollups" / "2026.jsonl").read_text(encoding="utf-8")
    assert '"new.old"' in rollups
    assert '"legacy"' not in rollups


@pytest.mark.skipif(os.name == "nt", reason="Unix link semantics")
@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_fold_refuses_linked_rollup_sidecar_without_touching_target(
    audit_root: Path, link_kind: str
) -> None:
    audit_file = audit_root / ".ai" / "memory" / "audit" / "2026.jsonl"
    audit_file.parent.mkdir(parents=True, exist_ok=True)
    raw = _make_audit_entry("old.action", _past_iso(40)) + "\n"
    audit_file.write_text(raw, encoding="utf-8")
    sidecar = audit_root / ".ai" / "memory" / "audit-rollups" / "2026.jsonl"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    external = audit_root / "external-rollup.jsonl"
    external.write_text('{"rollup_id":"external"}\n', encoding="utf-8")
    if link_kind == "symlink":
        sidecar.symlink_to(external)
    else:
        os.link(external, sidecar)
    original_external = external.read_bytes()

    result = fold_old_entries(audit_root, days=30)

    assert result["ok"] is False
    assert audit_file.read_text(encoding="utf-8") == raw
    assert external.read_bytes() == original_external
