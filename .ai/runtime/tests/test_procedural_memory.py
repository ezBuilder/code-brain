"""Tests for procedural_memory module."""

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from ai_core.procedural_memory import (
    append_procedure,
    consolidate_from_lessons,
    list_procedures,
    procedural_path,
    search_procedures,
)
from ai_core.lessons import add_lesson, lessons_path


class TestProceduralPath:
    """Test procedural_path() location."""

    def test_procedural_path_location(self):
        """Verify path is .ai/memory/procedural.jsonl."""
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = procedural_path(root)
            assert path == root / ".ai" / "memory" / "procedural.jsonl"

    def test_procedural_path_creates_parent(self):
        """Parent directory is created on append."""
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            result = append_procedure(
                root,
                kind="test",
                trigger="test_trigger",
                procedure="test procedure",
            )
            assert result["ok"]
            assert procedural_path(root).exists()


class TestAppendProcedure:
    """Test append_procedure() function."""

    def test_append_minimal(self):
        """Append with required fields only."""
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            result = append_procedure(
                root,
                kind="lesson",
                trigger="pytest_failure",
                procedure="Run pytest with verbose flag",
            )
            assert result["ok"]
            record = result["record"]
            assert record["kind"] == "lesson"
            assert record["trigger"] == "pytest_failure"
            assert record["procedure"] == "Run pytest with verbose flag"
            assert record["id"].startswith("proc-")
            assert "ts" in record
            assert record["tags"] == []
            assert record["evidence"] == {}

    def test_append_with_evidence_and_tags(self):
        """Append with evidence and tags."""
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            result = append_procedure(
                root,
                kind="skill_body",
                trigger="import_error",
                procedure="Add __init__.py to make directory a package",
                evidence={"source": "recommend.py", "id": "skill-123"},
                tags=["import", "packaging"],
            )
            assert result["ok"]
            record = result["record"]
            assert record["evidence"]["source"] == "recommend.py"
            assert record["tags"] == ["import", "packaging"]

    def test_append_cleans_text(self):
        """Text is cleaned (trimmed, redacted, etc.)."""
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            result = append_procedure(
                root,
                kind="  LESSON  ",
                trigger="  pytest_failure  ",
                procedure="  test procedure  ",
            )
            assert result["ok"]
            record = result["record"]
            assert record["kind"] == "lesson"
            assert record["trigger"] == "pytest_failure"
            assert record["procedure"] == "test procedure"

    def test_append_missing_kind(self):
        """Missing kind returns failure."""
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            result = append_procedure(
                root,
                kind="",
                trigger="test",
                procedure="test",
            )
            assert not result["ok"]
            assert result["reason"] == "missing_required_field"

    def test_append_missing_trigger(self):
        """Missing trigger returns failure."""
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            result = append_procedure(
                root,
                kind="lesson",
                trigger="",
                procedure="test",
            )
            assert not result["ok"]

    def test_append_missing_procedure(self):
        """Missing procedure returns failure."""
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            result = append_procedure(
                root,
                kind="lesson",
                trigger="test",
                procedure="",
            )
            assert not result["ok"]

    def test_append_dedup_tags(self):
        """Duplicate tags are deduplicated."""
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            result = append_procedure(
                root,
                kind="lesson",
                trigger="test",
                procedure="test",
                tags=["foo", "foo", "bar", "foo"],
            )
            assert result["ok"]
            assert set(result["record"]["tags"]) == {"foo", "bar"}


class TestListProcedures:
    """Test list_procedures() function."""

    def test_list_empty(self):
        """Empty file returns empty list."""
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            result = list_procedures(root)
            assert result["ok"]
            assert result["count"] == 0
            assert result["items"] == []

    def test_list_latest_first(self):
        """Records are returned latest-first."""
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            append_procedure(
                root, kind="a", trigger="t1", procedure="proc1", tags=["first"]
            )
            append_procedure(
                root, kind="b", trigger="t2", procedure="proc2", tags=["second"]
            )
            append_procedure(
                root, kind="c", trigger="t3", procedure="proc3", tags=["third"]
            )

            result = list_procedures(root)
            assert result["count"] == 3
            assert result["items"][0]["tags"] == ["third"]
            assert result["items"][1]["tags"] == ["second"]
            assert result["items"][2]["tags"] == ["first"]

    def test_list_limit(self):
        """Limit parameter works."""
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for i in range(10):
                append_procedure(
                    root,
                    kind="test",
                    trigger=f"trigger_{i}",
                    procedure=f"proc_{i}",
                )

            result = list_procedures(root, limit=3)
            assert result["count"] == 3
            assert len(result["items"]) == 3

    def test_list_filter_by_kind(self):
        """Filter by kind works."""
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            append_procedure(root, kind="lesson", trigger="t1", procedure="p1")
            append_procedure(root, kind="lesson", trigger="t2", procedure="p2")
            append_procedure(root, kind="skill_body", trigger="t3", procedure="p3")

            result = list_procedures(root, kind="lesson")
            assert result["count"] == 2
            assert all(item["kind"] == "lesson" for item in result["items"])

    def test_list_filter_by_trigger(self):
        """Filter by trigger works."""
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            append_procedure(root, kind="a", trigger="pytest_failure", procedure="p1")
            append_procedure(root, kind="b", trigger="pytest_failure", procedure="p2")
            append_procedure(root, kind="c", trigger="import_error", procedure="p3")

            result = list_procedures(root, trigger="pytest_failure")
            assert result["count"] == 2
            assert all(
                item["trigger"] == "pytest_failure" for item in result["items"]
            )

    def test_list_filter_case_insensitive(self):
        """Filters are case-insensitive."""
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            append_procedure(root, kind="Lesson", trigger="PyTest_Failure", procedure="p1")

            result = list_procedures(root, kind="LESSON", trigger="pytest_failure")
            assert result["count"] == 1


class TestSearchProcedures:
    """Test search_procedures() function."""

    def test_search_empty_query(self):
        """Empty query returns empty results."""
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            append_procedure(root, kind="test", trigger="test", procedure="proc")

            result = search_procedures(root, query="")
            assert result["ok"]
            assert result["count"] == 0

    def test_search_by_procedure_text(self):
        """Search matches procedure text."""
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            append_procedure(
                root, kind="a", trigger="t1", procedure="install pytest package"
            )
            append_procedure(
                root, kind="b", trigger="t2", procedure="run make clean"
            )

            result = search_procedures(root, query="pytest")
            assert result["count"] == 1
            assert "pytest" in result["items"][0]["procedure"]

    def test_search_by_trigger(self):
        """Search matches trigger."""
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            append_procedure(root, kind="a", trigger="import_error", procedure="p1")
            append_procedure(root, kind="b", trigger="runtime_error", procedure="p2")

            result = search_procedures(root, query="import")
            assert result["count"] == 1
            assert "import" in result["items"][0]["trigger"]

    def test_search_by_kind(self):
        """Search matches kind."""
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            append_procedure(root, kind="skill_body", trigger="t1", procedure="p1")
            append_procedure(root, kind="lesson", trigger="t2", procedure="p2")

            result = search_procedures(root, query="skill_body")
            assert result["count"] == 1
            assert result["items"][0]["kind"] == "skill_body"

    def test_search_by_tags(self):
        """Search matches tags."""
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            append_procedure(
                root, kind="a", trigger="t1", procedure="p1", tags=["networking"]
            )
            append_procedure(
                root, kind="b", trigger="t2", procedure="p2", tags=["storage"]
            )

            result = search_procedures(root, query="networking")
            assert result["count"] == 1
            assert "networking" in result["items"][0]["tags"]

    def test_search_multi_token(self):
        """Search with multiple tokens (OR semantics)."""
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            append_procedure(
                root,
                kind="lesson",
                trigger="pytest",
                procedure="Run pytest with verbose",
            )
            append_procedure(
                root, kind="skill", trigger="docker", procedure="Docker commands"
            )

            result = search_procedures(root, query="pytest docker")
            assert result["count"] == 2

    def test_search_ranking_by_score(self):
        """Results are ranked by relevance score."""
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            # "pytest" in procedure (weight 2.0)
            append_procedure(
                root, kind="a", trigger="t1", procedure="Run pytest verbose"
            )
            # "pytest" in trigger (weight 1.5)
            append_procedure(root, kind="b", trigger="pytest_failure", procedure="p2")
            # "pytest" in kind (weight 1.0)
            append_procedure(root, kind="pytest_tool", trigger="t3", procedure="p3")

            result = search_procedures(root, query="pytest")
            assert result["count"] == 3
            # First should have pytest in procedure
            assert "pytest" in result["items"][0]["procedure"]


class TestConsolidateFromLessons:
    """Test consolidate_from_lessons() function."""

    def test_consolidate_dry_run_no_write(self):
        """Dry run returns preview without writing."""
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            add_lesson(
                root,
                source="test",
                failure="import failed",
                cause="missing module",
                fix="pip install module",
            )

            result = consolidate_from_lessons(root, dry_run=True)
            assert result["ok"]
            assert result["merged"] == 1
            assert "preview" in result

            # Procedural file should not exist yet
            assert not procedural_path(root).exists()

    def test_consolidate_write(self):
        """Consolidate writes lessons to procedural."""
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            add_lesson(
                root,
                source="test_source",
                failure="import failed",
                cause="missing module",
                fix="pip install module",
            )

            result = consolidate_from_lessons(root)
            assert result["ok"]
            assert result["merged"] == 1

            # Verify procedural has the record
            proc_result = list_procedures(root)
            assert proc_result["count"] == 1
            proc_rec = proc_result["items"][0]
            assert proc_rec["kind"] == "lesson"
            assert proc_rec["trigger"] == "test_source"
            assert "import failed" in proc_rec["procedure"]
            assert proc_rec["evidence"]["source"] == "lessons"

    def test_consolidate_dedup_by_source(self):
        """Same source lessons are deduplicated (latest wins)."""
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            # Add two lessons with same source
            add_lesson(
                root,
                source="same",
                failure="first failure",
                cause="first cause",
                fix="first fix",
            )
            add_lesson(
                root,
                source="same",
                failure="second failure",
                cause="second cause",
                fix="second fix",
            )

            result = consolidate_from_lessons(root)
            assert result["ok"]
            assert result["merged"] == 1
            # Only latest is kept
            proc_result = list_procedures(root, kind="lesson")
            assert proc_result["count"] == 1
            assert "second failure" in proc_result["items"][0]["procedure"]

    def test_consolidate_multiple_sources(self):
        """Multiple different sources are consolidated separately."""
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            add_lesson(
                root,
                source="source_a",
                failure="fail_a",
                cause="cause_a",
                fix="fix_a",
            )
            add_lesson(
                root,
                source="source_b",
                failure="fail_b",
                cause="cause_b",
                fix="fix_b",
            )

            result = consolidate_from_lessons(root)
            assert result["ok"]
            assert result["merged"] == 2

            proc_result = list_procedures(root)
            assert proc_result["count"] == 2

    def test_consolidate_preserves_tags(self):
        """Tags from lessons are preserved in procedural."""
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            add_lesson(
                root,
                source="test",
                failure="fail",
                cause="cause",
                fix="fix",
                tags=["testing", "pytest"],
            )

            consolidate_from_lessons(root)
            proc_result = list_procedures(root)
            assert "testing" in proc_result["items"][0]["tags"]
            assert "pytest" in proc_result["items"][0]["tags"]

    def test_consolidate_invalid_lessons_ignored(self):
        """Malformed lessons.jsonl lines are silently ignored."""
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            # Add one valid lesson
            add_lesson(
                root,
                source="test",
                failure="fail",
                cause="cause",
                fix="fix",
            )
            # Corrupt the file with a bad line
            lessons_file = lessons_path(root)
            with lessons_file.open("a") as f:
                f.write("{invalid json\n")

            # Should not crash
            result = consolidate_from_lessons(root)
            assert result["ok"]
            # Should process the valid lesson
            assert result["merged"] >= 1


class TestIntegration:
    """Integration tests across multiple functions."""

    def test_append_list_search_round_trip(self):
        """Full cycle: append, list, search."""
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            # Append procedures
            append_procedure(
                root,
                kind="lesson",
                trigger="pytest_failure",
                procedure="Run pytest with -v flag",
                tags=["testing"],
            )
            append_procedure(
                root,
                kind="skill_body",
                trigger="import_error",
                procedure="Add __init__.py to directories",
                tags=["import"],
            )

            # List all
            list_result = list_procedures(root)
            assert list_result["count"] == 2

            # Search for pytest
            search_result = search_procedures(root, query="pytest")
            assert search_result["count"] == 1
            assert "pytest" in search_result["items"][0]["procedure"]

            # Filter by kind
            skill_result = list_procedures(root, kind="skill_body")
            assert skill_result["count"] == 1
            assert skill_result["items"][0]["kind"] == "skill_body"
