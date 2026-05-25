from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core.obs import mem_eval_summary  # noqa: E402


def test_mem_eval_summary_empty_state(tmp_path: Path) -> None:
    """Empty audit and lessons files return zero counts and ok=True."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".ai" / "memory" / "audit").mkdir(parents=True)
    (repo / ".ai" / "memory" / "lessons.jsonl").touch()

    result = mem_eval_summary(repo, window_days=7)
    assert result["ok"] is True
    assert result["window_days"] == 7
    assert all(v["accept"] == 0 and v["reject"] == 0 for v in result["accept_rate_by_day"].values())
    assert all(v == 0 for v in result["hot_audit_by_day"].values())
    assert result["search_index_age_seconds"] is None
    assert result["lessons_added_recent"] == 0


def test_mem_eval_summary_missing_files(tmp_path: Path) -> None:
    """Missing audit and lessons files do not crash; return zero counts."""
    repo = tmp_path / "repo"
    repo.mkdir()

    result = mem_eval_summary(repo, window_days=7)
    assert result["ok"] is True
    assert result["lessons_added_recent"] == 0
    assert result["search_index_age_seconds"] is None


def test_mem_eval_summary_accept_reject_tracking(tmp_path: Path) -> None:
    """Accept/reject actions are counted per day."""
    repo = tmp_path / "repo"
    repo.mkdir()
    audit_dir = repo / ".ai" / "memory" / "audit"
    audit_dir.mkdir(parents=True)

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    # Write audit entries for today and yesterday
    audit_file = audit_dir / f"{now.year}.jsonl"
    entries = [
        {
            "ts": now.isoformat().replace("+00:00", "Z"),
            "action": "skill.recommend_pending",
            "category": "recommend",
            "payload": {"id": "skill-abc"},
        },
        {
            "ts": now.isoformat().replace("+00:00", "Z"),
            "action": "skill.accept_install",
            "category": "recommend",
            "payload": {"id": "skill-abc"},
        },
        {
            "ts": now.isoformat().replace("+00:00", "Z"),
            "action": "skill.accept_install",
            "category": "recommend",
            "payload": {"id": "skill-def"},
        },
        {
            "ts": (now - timedelta(days=1)).isoformat().replace("+00:00", "Z"),
            "action": "skill.reject",
            "category": "recommend",
            "payload": {"id": "agent-xyz"},
        },
    ]
    with audit_file.open("a", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, sort_keys=True) + "\n")

    result = mem_eval_summary(repo, window_days=7)
    assert result["ok"] is True
    assert result["accept_rate_by_day"][today]["accept"] == 2
    assert result["accept_rate_by_day"][today]["reject"] == 0
    assert result["accept_rate_by_day"][yesterday]["accept"] == 0
    assert result["accept_rate_by_day"][yesterday]["reject"] == 1


def test_mem_eval_summary_hot_audit_by_day(tmp_path: Path) -> None:
    """Hot-tier pressure counts recommend_pending actions per day."""
    repo = tmp_path / "repo"
    repo.mkdir()
    audit_dir = repo / ".ai" / "memory" / "audit"
    audit_dir.mkdir(parents=True)

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

    audit_file = audit_dir / f"{now.year}.jsonl"
    # Three recommend_pending entries today
    with audit_file.open("a", encoding="utf-8") as f:
        for i in range(3):
            entry = {
                "ts": now.isoformat().replace("+00:00", "Z"),
                "action": "agent.recommend_pending",
                "category": "recommend",
                "payload": {"id": f"agent-{i}"},
            }
            f.write(json.dumps(entry, sort_keys=True) + "\n")

    result = mem_eval_summary(repo, window_days=7)
    assert result["hot_audit_by_day"][today] == 3


def test_mem_eval_summary_lessons_counted(tmp_path: Path) -> None:
    """Lessons added within window are counted."""
    repo = tmp_path / "repo"
    repo.mkdir()
    lessons_dir = repo / ".ai" / "memory"
    lessons_dir.mkdir(parents=True)

    now = datetime.now(timezone.utc)
    lessons_file = lessons_dir / "lessons.jsonl"

    # Two lessons today, one from 8 days ago (outside window)
    entries = [
        {
            "id": "lesson-1",
            "created_at": now.isoformat().replace("+00:00", "Z"),
            "kind": "precall",
            "outcome": "accept",
        },
        {
            "id": "lesson-2",
            "created_at": now.isoformat().replace("+00:00", "Z"),
            "kind": "precall",
            "outcome": "reject",
        },
        {
            "id": "lesson-old",
            "created_at": (now - timedelta(days=8)).isoformat().replace("+00:00", "Z"),
            "kind": "precall",
            "outcome": "accept",
        },
    ]
    with lessons_file.open("a", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, sort_keys=True) + "\n")

    result = mem_eval_summary(repo, window_days=7)
    assert result["lessons_added_recent"] == 2


def test_mem_eval_summary_malformed_json_ignored(tmp_path: Path) -> None:
    """Malformed JSON lines are silently skipped."""
    repo = tmp_path / "repo"
    repo.mkdir()
    audit_dir = repo / ".ai" / "memory" / "audit"
    audit_dir.mkdir(parents=True)

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

    audit_file = audit_dir / f"{now.year}.jsonl"
    # Mix valid and malformed lines
    with audit_file.open("w", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": now.isoformat().replace("+00:00", "Z"),
            "action": "skill.accept_install",
            "category": "recommend",
            "payload": {"id": "skill-1"},
        }, sort_keys=True) + "\n")
        f.write("this is not json\n")
        f.write(json.dumps({
            "ts": now.isoformat().replace("+00:00", "Z"),
            "action": "skill.reject",
            "category": "recommend",
            "payload": {"id": "skill-2"},
        }, sort_keys=True) + "\n")

    result = mem_eval_summary(repo, window_days=7)
    assert result["accept_rate_by_day"][today]["accept"] == 1
    assert result["accept_rate_by_day"][today]["reject"] == 1


def test_mem_eval_summary_window_boundary(tmp_path: Path) -> None:
    """Entries outside the window are excluded; all days in window are present."""
    repo = tmp_path / "repo"
    repo.mkdir()
    audit_dir = repo / ".ai" / "memory" / "audit"
    audit_dir.mkdir(parents=True)

    now = datetime.now(timezone.utc)
    audit_file = audit_dir / f"{now.year}.jsonl"

    # Entry from 9 days ago (outside 7-day window)
    old_entry = {
        "ts": (now - timedelta(days=9)).isoformat().replace("+00:00", "Z"),
        "action": "skill.accept_install",
        "category": "recommend",
        "payload": {"id": "old"},
    }
    audit_file.write_text(json.dumps(old_entry, sort_keys=True) + "\n", encoding="utf-8")

    result = mem_eval_summary(repo, window_days=7)
    assert result["ok"] is True
    # Old entry should not be counted
    assert all(v["accept"] == 0 and v["reject"] == 0 for v in result["accept_rate_by_day"].values())
    # All 7 days should be present in the dict
    assert len(result["accept_rate_by_day"]) == 7
    assert len(result["hot_audit_by_day"]) == 7


def test_mem_eval_summary_search_index_age(tmp_path: Path) -> None:
    """Search index age is computed from chunks.updated_at."""
    repo = tmp_path / "repo"
    repo.mkdir()
    import sqlite3

    db_path = repo / ".ai" / "cache" / "code.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Create a minimal chunks table
    now = datetime.now(timezone.utc)
    past_time = (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")

    with sqlite3.connect(db_path) as conn:
        conn.execute("create table chunks (id integer primary key, path text, sha256 text, summary text, updated_at text)")
        conn.execute("insert into chunks (path, sha256, summary, updated_at) values (?, ?, ?, ?)",
                    ("test.py", "abc123", "Test module", past_time))
        conn.commit()

    result = mem_eval_summary(repo, window_days=7)
    assert result["ok"] is True
    # Age should be approximately 3600 seconds (1 hour), allowing ±60s tolerance
    assert result["search_index_age_seconds"] is not None
    assert 3540 <= result["search_index_age_seconds"] <= 3660
