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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from ai_core import audit_fold
from ai_core.audit_fold import fold_old_entries
from ai_core.doctor import check_audit_chain, check_audit_index


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

        result = fold_old_entries(audit_root, days=30)
        assert result["ok"] is True
        assert result["folded_days"] >= 1  # At least one date folded
        assert result["removed_entries"] == 2
        assert result["added_fold_records"] >= 1

        # Verify file contains recent + fold records
        content = audit_file.read_text(encoding="utf-8")
        new_lines = content.splitlines()
        assert len(new_lines) >= 2  # 1 recent + at least 1 fold

        # Last record should be a fold
        last_line = new_lines[-1]
        last_entry = json.loads(last_line)
        assert last_entry.get("action") == "_folded"
        assert "counts" in last_entry.get("payload", {})


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
        assert result["removed_entries"] == 1  # Would remove

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
        assert result["removed_entries"] >= 2  # At least 2 old entries removed
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
        assert result["removed_entries"] == 3

        # Parse final file
        content = audit_file.read_text(encoding="utf-8")
        fold_lines = [l for l in content.splitlines() if l.strip()]
        fold_entries = [json.loads(l) for l in fold_lines]

        fold_recs = [e for e in fold_entries if e.get("action") == "_folded"]
        assert len(fold_recs) == 2

        for fold in fold_recs:
            assert "ts" in fold
            assert fold["ts"].endswith("T23:59:59Z")
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

        result = fold_old_entries(audit_root, days=30)
        assert result["ok"] is True
        assert result["removed_entries"] == 1

        # Result should still be valid
        new_lines = audit_file.read_text(encoding="utf-8").splitlines()
        for line in new_lines:
            if line.strip():
                json.loads(line)  # Should parse


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


class TestStreamingAndAtomicity:
    def test_fold_streams_without_path_read_text(
        self,
        audit_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        audit_file = audit_root / ".ai" / "memory" / "audit" / "2026.jsonl"
        original_read_text = Path.read_text
        audit_file.write_text(
            _make_audit_entry("old.action", _past_iso(40))
            + "\n"
            + _make_audit_entry("recent.action", _now_iso())
            + "\n",
            encoding="utf-8",
        )

        def guarded_read_text(self: Path, *args, **kwargs):
            if self == audit_file:
                raise AssertionError("audit folding must stream source JSONL")
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", guarded_read_text)

        result = fold_old_entries(audit_root, days=30)

        assert result["ok"] is True
        assert result["removed_entries"] == 1
        content = original_read_text(audit_file, encoding="utf-8")
        assert "recent.action" in content
        assert '"action":"_folded"' in content

    def test_fold_rechains_and_rebuilds_index(self, audit_root: Path) -> None:
        audit_file = audit_root / ".ai" / "memory" / "audit" / "2026.jsonl"
        audit_file.write_text(
            _make_audit_entry("old.action", _past_iso(40))
            + "\n"
            + _make_audit_entry("recent.action", _now_iso())
            + "\n",
            encoding="utf-8",
        )

        result = fold_old_entries(audit_root, days=30)

        assert result["ok"] is True
        assert result["audit_index"]["ok"] is True
        assert check_audit_chain(audit_root).ok is True
        assert check_audit_index(audit_root).ok is True
        records = [json.loads(line) for line in audit_file.read_text(encoding="utf-8").splitlines()]
        assert all("prev_sha" in record for record in records)

    def test_fold_oversized_source_line_fails_without_mutation(
        self,
        audit_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        audit_file = audit_root / ".ai" / "memory" / "audit" / "2026.jsonl"
        original = (
            json.dumps(
                {
                    "ts": _past_iso(40),
                    "action": "old.action",
                    "category": "test",
                    "payload": {"blob": "x" * 4096},
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        audit_file.write_bytes(original)
        monkeypatch.setattr(audit_fold, "AUDIT_LINE_MAX_BYTES", 256)

        result = fold_old_entries(audit_root, days=30)

        assert result["ok"] is False
        assert "audit line exceeds" in result["errors"][0]
        assert audit_file.read_bytes() == original

    def test_fold_output_limit_preserves_previous_file(
        self,
        audit_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        audit_file = audit_root / ".ai" / "memory" / "audit" / "2026.jsonl"
        original = (_make_audit_entry("old.action", _past_iso(40)) + "\n").encode("utf-8")
        audit_file.write_bytes(original)
        monkeypatch.setattr(audit_fold, "AUDIT_MAX_BYTES", len(original) + 8)

        result = fold_old_entries(audit_root, days=30)

        assert result["ok"] is False
        assert "private write exceeds" in result["errors"][0]
        assert result["removed_entries"] == 0
        assert audit_file.read_bytes() == original
        assert list(audit_file.parent.glob(f".{audit_file.name}.*.tmp")) == []
