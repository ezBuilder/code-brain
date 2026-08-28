"""Regression: a targeted incremental rebuild must retire rows git no longer reports.

Observed in a real consumer install: 2,119 rows (a leftover linked-worktree copy
under ``.claude/worktrees/`` plus ``.ai/tmp/`` scratch) stayed in the index after
becoming gitignored. ``index_hash_status`` reported them as changed on every
query and the targeted rebuild re-affirmed them as ``unchanged``, so the pair
never converged: every query paid a full re-affirm pass (p95 2.26s) and the
polluted rows outranked real project files in results.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ai_core import search as search_mod


def _init_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "ghost@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Ghost Test"], cwd=root, check=True)


def _make_repo(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    (root / ".ai").mkdir(parents=True)
    (root / ".ai" / "config.yaml").write_text("project_name: ghost-convergence\n", encoding="utf-8")
    tracked = root / "src" / "main.py"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("TrackedNeedle = True\n", encoding="utf-8")
    _init_repo(root)
    subprocess.run(["git", "add", "src/main.py"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=root, check=True)
    return root, tracked


def _indexed_paths(root: Path) -> set[str]:
    with search_mod._connection_scope(root) as conn:
        search_mod.init_schema(conn)
        return {str(row[0]) for row in conn.execute("select path from file_state")}


def test_targeted_rebuild_retires_rows_git_no_longer_reports(tmp_path: Path) -> None:
    root, _tracked = _make_repo(tmp_path)

    # A path that git reports now, so it gets indexed normally...
    ghost = root / "scratch" / "leftover.py"
    ghost.parent.mkdir(parents=True)
    ghost.write_text("GhostNeedle = True\n", encoding="utf-8")

    assert search_mod.rebuild(root)["ok"] is True
    assert "scratch/leftover.py" in _indexed_paths(root)

    # ...and then stops being a candidate, exactly like a directory that becomes
    # ignored. The file still exists and still parses as indexable text, which is
    # what used to keep it pinned in the index forever.
    (root / ".gitignore").write_text("scratch/\n", encoding="utf-8")
    assert search_mod._is_indexable_text_file(root, ghost) is True
    assert "scratch/leftover.py" not in {
        p.relative_to(root).as_posix()
        for p in search_mod.candidate_files(root, use_cache=False, update_cache=False)
    }

    status = search_mod.index_hash_status(root)
    changed = set(status.get("changed_paths") or [])
    assert "scratch/leftover.py" in changed

    result = search_mod.rebuild(root, incremental=True, paths=changed)
    assert result["ok"] is True
    assert result["deleted"] >= 1

    # Converged: the row is gone and a second status pass is clean.
    assert "scratch/leftover.py" not in _indexed_paths(root)
    after = search_mod.index_hash_status(root)
    assert after["ok"] is True, after
    assert not (after.get("changed_paths") or [])


def test_targeted_rebuild_keeps_rows_when_git_cannot_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-safe: never retire rows on an inconclusive git answer."""
    root, _tracked = _make_repo(tmp_path)
    ghost = root / "scratch" / "leftover.py"
    ghost.parent.mkdir(parents=True)
    ghost.write_text("GhostNeedle = True\n", encoding="utf-8")
    assert search_mod.rebuild(root)["ok"] is True

    (root / ".gitignore").write_text("scratch/\n", encoding="utf-8")
    monkeypatch.setattr(search_mod, "_git_candidate_rels", lambda _root: None)

    result = search_mod.rebuild(root, incremental=True, paths={"scratch/leftover.py"})
    assert result["ok"] is True
    assert result["deleted"] == 0
    assert "scratch/leftover.py" in _indexed_paths(root)


def test_untracked_new_file_is_still_indexed(tmp_path: Path) -> None:
    """Guard against over-retiring: untracked-but-visible files must survive."""
    root, _tracked = _make_repo(tmp_path)
    assert search_mod.rebuild(root)["ok"] is True

    fresh = root / "src" / "fresh.py"
    fresh.write_text("FreshNeedle = True\n", encoding="utf-8")

    status = search_mod.index_hash_status(root)
    changed = set(status.get("changed_paths") or [])
    assert "src/fresh.py" in changed

    result = search_mod.rebuild(root, incremental=True, paths=changed)
    assert result["ok"] is True
    assert "src/fresh.py" in _indexed_paths(root)
    assert search_mod.index_hash_status(root)["ok"] is True


def test_auto_refresh_does_not_reindex_ignored_copy_of_staged_deletion(tmp_path: Path) -> None:
    """A managed file staged for deletion must not oscillate in and out forever."""

    root, _tracked = _make_repo(tmp_path)
    managed = root / ".agents" / "hooks.json"
    managed.parent.mkdir(parents=True)
    managed.write_text('{"hooks": {}}\n', encoding="utf-8")
    subprocess.run(["git", "add", ".agents/hooks.json"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "track managed hook"], cwd=root, check=True)
    assert search_mod.rebuild(root)["ok"] is True
    assert ".agents/hooks.json" in _indexed_paths(root)

    (root / ".gitignore").write_text(".agents/\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=root, check=True)
    subprocess.run(["git", "rm", "--cached", ".agents/hooks.json"], cwd=root, check=True)
    assert managed.is_file(), "the ignored working-tree copy is intentionally retained"
    assert ".agents/hooks.json" not in search_mod._git_dirty_paths(root)

    # Establish the authoritative post-deletion index once, then prove that the
    # read path neither resurrects the ignored row nor advances the generation.
    assert search_mod.rebuild(root)["ok"] is True
    search_mod._auto_refresh_if_stale(root)  # one metadata warm-up
    generation = (root / ".ai" / "cache" / "code-index-generation").read_bytes()
    for _ in range(3):
        result = search_mod._auto_refresh_if_stale(root)
        assert result == {"enabled": True, "rebuilt": False, "reason": "current"}
        assert ".agents/hooks.json" not in _indexed_paths(root)
        assert search_mod.index_hash_status(root)["ok"] is True
        assert (root / ".ai" / "cache" / "code-index-generation").read_bytes() == generation
