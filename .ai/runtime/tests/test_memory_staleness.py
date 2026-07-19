"""Tests for git-vs-recorded-memory staleness detection (cross-agent divergence fix).

Reproduces the navio incident: agents stopped calling record tools, so
session-current.md / decisions.jsonl froze while git advanced. memory_freshness must
flag that gap so the SessionStart banner converges every agent on git truth.
"""
from __future__ import annotations

import subprocess
import sys
import os
from pathlib import Path

import pytest

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


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_memory_freshness_never_reads_external_symlink_timestamps(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "feature_a.txt", "feat: a")
    mem = tmp_path / ".ai" / "memory"
    mem.mkdir(parents=True)
    external_note = tmp_path / "external-session.md"
    external_note.write_text(
        "# Current Session\n\n- [2099-01-01T00:00:00Z] EXTERNAL_FRESHNESS\n",
        encoding="utf-8",
    )
    external_decisions = tmp_path / "external-decisions.jsonl"
    external_decisions.write_text(
        '{"decided_at":"2099-01-01T00:00:00Z","decision":"EXTERNAL_FRESHNESS"}\n',
        encoding="utf-8",
    )
    (mem / "session-current.md").symlink_to(external_note)
    (mem / "decisions.jsonl").symlink_to(external_decisions)

    info = memory_freshness(tmp_path)

    assert info["last_recorded"] == ""
    assert info["stale"] is True
    assert "EXTERNAL_FRESHNESS" not in staleness_banner(tmp_path)


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_memory_freshness_never_reads_external_hardlink_timestamps(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "feature_a.txt", "feat: a")
    mem = tmp_path / ".ai" / "memory"
    mem.mkdir(parents=True)
    external = tmp_path / "external-session.md"
    external.write_text(
        "# Current Session\n\n- [2099-01-01T00:00:00Z] EXTERNAL_HARDLINK\n",
        encoding="utf-8",
    )
    os.link(external, mem / "session-current.md")

    info = memory_freshness(tmp_path)

    assert info["last_recorded"] == ""
    assert info["stale"] is True


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


# ---- P3: remote-ahead (cb-behind) detection across machines ----

def test_remote_sync_detects_remote_ahead(tmp_path: Path) -> None:
    """Simulate the VPS pushing a commit the Mac has not pulled: after a fetch the
    local sees origin ahead, which remote_sync must report as behind=1."""
    from ai_core.memory_staleness import remote_sync_banner, remote_sync_state

    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "-q")

    work = tmp_path / "work"
    work.mkdir()
    _init_repo(work)
    _git(work, "remote", "add", "origin", str(remote))
    _commit(work, "a.txt", "c1")
    _git(work, "branch", "-M", "develop")
    _git(work, "push", "-q", "-u", "origin", "develop")

    # second clone = "the VPS" pushes a new commit
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(remote), str(other))
    _git(other, "config", "user.email", "t@t.test")
    _git(other, "config", "user.name", "t")
    _git(other, "checkout", "-q", "develop")  # bare HEAD may not point at develop
    _commit(other, "b.txt", "c2 from vps")
    _git(other, "push", "-q", "origin", "develop")

    # the Mac fetches (sleep-time job) but has NOT pulled
    _git(work, "fetch", "-q", "origin")
    st = remote_sync_state(work)
    assert st["behind"] == 1 and st["ahead"] == 0, st
    banner = remote_sync_banner(work)
    assert "cb-behind" in banner and "1커밋" in banner


def test_remote_sync_silent_without_upstream(tmp_path: Path) -> None:
    from ai_core.memory_staleness import remote_sync_banner, remote_sync_state

    repo = tmp_path / "r"
    repo.mkdir()
    _init_repo(repo)
    _commit(repo, "a.txt", "c1")
    # no upstream configured → no banner, behind=0
    assert remote_sync_state(repo)["behind"] == 0
    assert remote_sync_banner(repo) == ""
