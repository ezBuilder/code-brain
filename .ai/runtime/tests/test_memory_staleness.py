"""Tests for git-vs-recorded-memory staleness detection (cross-agent divergence fix).

Reproduces the navio incident: agents stopped calling record tools, so
session-current.md / decisions.jsonl froze while git advanced. memory_freshness must
flag that gap so the SessionStart banner converges every agent on git truth.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core.memory_staleness import (  # noqa: E402
    DIRTY_STALE_THRESHOLD,
    memory_freshness,
    staleness_banner,
)
from ai_core.hooks import _auto_milestone_on_stale  # noqa: E402


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(repo: Path) -> None:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.test")
    _git(repo, "config", "user.name", "t")


def _commit(repo: Path, name: str, message: str) -> None:
    (repo / name).write_text("x", encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", message)


def _write_note(repo: Path, iso: str, text: str = "milestone") -> None:
    mem = repo / ".ai" / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "session-current.md").write_text(
        f"# Current Session\n\n- [{iso}] {text}\n", encoding="utf-8"
    )


def test_not_git_is_not_stale(tmp_path: Path) -> None:
    info = memory_freshness(tmp_path)
    assert info["git"] is False
    assert info["stale"] is False
    assert staleness_banner(tmp_path) == ""


def test_commits_after_last_record_flag_stale(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_note(tmp_path, "2020-01-01T00:00:00Z", "ancient milestone")
    _commit(tmp_path, "feature_a.txt", "feat: a")
    _commit(tmp_path, "feature_b.txt", "feat: b")

    info = memory_freshness(tmp_path)
    assert info["git"] is True
    assert info["stale"] is True
    assert info["commit_count"] >= 2
    subjects = {c["subject"] for c in info["commits"]}
    assert "feat: a" in subjects and "feat: b" in subjects

    banner = staleness_banner(tmp_path)
    assert banner.startswith("cb-stale:")
    assert "git log/status" in banner


def test_recent_record_clean_tree_is_fresh(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "feature_a.txt", "feat: a")
    # Recorded milestone is dated far in the future, so no commit is "since" it.
    _write_note(tmp_path, "2099-01-01T00:00:00Z", "up to date")

    info = memory_freshness(tmp_path)
    assert info["git"] is True
    assert info["commit_count"] == 0
    assert info["stale"] is False
    assert staleness_banner(tmp_path) == ""


def test_large_dirty_tree_flags_stale_even_when_committed_history_recorded(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "feature_a.txt", "feat: a")
    _write_note(tmp_path, "2099-01-01T00:00:00Z", "up to date")
    for i in range(DIRTY_STALE_THRESHOLD):
        (tmp_path / f"dirty_{i}.txt").write_text("wip", encoding="utf-8")

    info = memory_freshness(tmp_path)
    assert info["commit_count"] == 0  # nothing committed since the recorded milestone
    assert info["dirty_count"] >= DIRTY_STALE_THRESHOLD
    assert info["stale"] is True
    assert "dirty" in staleness_banner(tmp_path)


def test_decisions_jsonl_timestamp_also_counts_as_recorded(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "feature_a.txt", "feat: a")
    mem = tmp_path / ".ai" / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "decisions.jsonl").write_text(
        '{"decided_at":"2099-01-01T00:00:00Z","decision":"locked"}\n', encoding="utf-8"
    )

    info = memory_freshness(tmp_path)
    assert info["last_recorded"] == "2099-01-01T00:00:00Z"
    assert info["commit_count"] == 0
    assert info["stale"] is False


def _session_note_text(repo: Path) -> str:
    note = repo / ".ai" / "memory" / "session-current.md"
    return note.read_text(encoding="utf-8") if note.is_file() else ""


def test_auto_milestone_records_git_facts_when_stale(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write_note(tmp_path, "2020-01-01T00:00:00Z", "ancient")
    _commit(tmp_path, "feature_a.txt", "feat: shiny new thing")

    wrote = _auto_milestone_on_stale(tmp_path)
    assert wrote is True
    text = _session_note_text(tmp_path)
    assert "[auto:" in text
    # Factual git data, not an LLM summary.
    assert "feat: shiny new thing" in text


def test_auto_milestone_is_deduped(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "base.txt", "init")  # need a HEAD for staleness to evaluate
    _write_note(tmp_path, "2099-01-01T00:00:00Z", "current")
    for i in range(DIRTY_STALE_THRESHOLD):
        (tmp_path / f"dirty_{i}.txt").write_text("wip", encoding="utf-8")

    assert _auto_milestone_on_stale(tmp_path) is True
    _auto_milestone_on_stale(tmp_path)  # second call: same HEAD, must not duplicate
    assert _session_note_text(tmp_path).count("[auto:") == 1


def test_auto_milestone_noop_when_fresh(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "feature_a.txt", "feat: a")
    _write_note(tmp_path, "2099-01-01T00:00:00Z", "current")

    assert _auto_milestone_on_stale(tmp_path) is False
    assert "[auto:" not in _session_note_text(tmp_path)


def test_auto_milestone_respects_optout(tmp_path: Path, monkeypatch) -> None:
    _init_repo(tmp_path)
    _write_note(tmp_path, "2020-01-01T00:00:00Z", "ancient")
    _commit(tmp_path, "feature_a.txt", "feat: a")

    monkeypatch.setenv("AI_AUTO_MILESTONE_ON_STALE", "0")
    assert _auto_milestone_on_stale(tmp_path) is False
    assert "[auto:" not in _session_note_text(tmp_path)
