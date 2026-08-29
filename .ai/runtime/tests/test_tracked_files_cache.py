from __future__ import annotations

import os
import json
import stat
import subprocess
from pathlib import Path

import pytest

from ai_core import tracked_files as tracked


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "tracked@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "tracked"], cwd=repo, check=True)
    first = repo / "first.py"
    first.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "first.py"], cwd=repo, check=True)
    return repo


def test_tracked_file_cache_hit_avoids_second_git_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    first = tracked.tracked_files(repo)

    def unexpected_git(*_args, **_kwargs):
        raise AssertionError("unchanged git index must reuse tracked-file cache")

    monkeypatch.setattr(tracked.subprocess, "run", unexpected_git)
    second = tracked.tracked_files(repo)

    assert second == first
    assert (repo / ".ai" / "cache" / "tracked-files.json").is_file()


def test_tracked_file_cache_invalidates_when_git_index_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    tracked.tracked_files(repo)
    second = repo / "second.py"
    second.write_text("VALUE = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "second.py"], cwd=repo, check=True)
    real_run = subprocess.run
    calls = {"git": 0}

    def counting_git(*args, **kwargs):
        calls["git"] += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(tracked.subprocess, "run", counting_git)
    paths = tracked.tracked_files(repo)

    assert second in paths
    assert calls["git"] == 1


def test_tracked_file_cache_filters_deleted_worktree_file(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first = repo / "first.py"
    tracked.tracked_files(repo)
    first.unlink()

    paths = tracked.tracked_files(repo)

    assert first not in paths


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_tracked_files_include_broken_symlink_entry(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    linked = repo / "broken-link"
    linked.symlink_to("missing-target")
    subprocess.run(["git", "add", "broken-link"], cwd=repo, check=True)

    paths = tracked.tracked_files(repo, use_cache=False, update_cache=False)

    assert linked in paths


def test_ci_read_does_not_create_tracked_file_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setenv("CI", "true")

    paths = tracked.tracked_files(repo)

    assert paths
    assert not (repo / ".ai" / "cache" / "tracked-files.json").exists()


def test_git_failure_never_falls_back_to_recursive_worktree_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    local_noise = repo / ".chatgpt2codex" / "session.json"
    local_noise.parent.mkdir()
    local_noise.write_text("token=" + "n" * 24 + "\n", encoding="utf-8")

    def unavailable_git(*_args, **_kwargs):
        raise OSError("git unavailable")

    monkeypatch.setattr(tracked.subprocess, "run", unavailable_git)

    with pytest.raises(tracked.GitBaselineUnavailable, match="baseline unavailable"):
        tracked.tracked_files(repo, use_cache=False, update_cache=False)


def test_non_git_filesystem_baseline_excludes_internal_runtime_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "src" / "main.py"
    source.parent.mkdir()
    source.write_text("VALUE = 1\n", encoding="utf-8")
    ignored = [
        repo / ".chatgpt2codex" / "session.json",
        repo / ".ai" / "memory" / "events.jsonl",
        repo / ".ai" / "cache" / "state.json",
        repo / ".code-brain-install-transaction" / "owner.json",
        repo / ".venv" / "secret.py",
        repo / "node_modules" / "pkg" / "index.js",
    ]
    for path in ignored:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("token=" + "i" * 24 + "\n", encoding="utf-8")

    def unexpected_git(*_args, **_kwargs):
        raise AssertionError("non-Git project must not inherit or invoke parent Git")

    monkeypatch.setattr(tracked.subprocess, "run", unexpected_git)
    paths = tracked.tracked_files(repo, use_cache=False, update_cache=False)

    assert paths.source == "filesystem"
    assert source in paths
    assert all(path not in paths for path in ignored)


def test_nested_non_git_project_does_not_inherit_parent_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=parent, check=True)
    subprocess.run(["git", "config", "user.email", "parent@example.com"], cwd=parent, check=True)
    subprocess.run(["git", "config", "user.name", "parent"], cwd=parent, check=True)
    parent_file = parent / "parent.py"
    parent_file.write_text("PARENT = True\n", encoding="utf-8")
    subprocess.run(["git", "add", "parent.py"], cwd=parent, check=True)
    project = parent / "nested-project"
    project.mkdir()
    source = project / "local.py"
    source.write_text("LOCAL = True\n", encoding="utf-8")

    def unexpected_git(*_args, **_kwargs):
        raise AssertionError("nested project without .git must use its own filesystem baseline")

    monkeypatch.setattr(tracked.subprocess, "run", unexpected_git)
    paths = tracked.tracked_files(project, use_cache=False, update_cache=False)

    assert paths.source == "filesystem"
    assert paths == [source]


def test_valid_cache_can_serve_incremental_path_when_git_command_is_temporarily_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    expected = tracked.tracked_files(repo)

    def unavailable_git(*_args, **_kwargs):
        raise AssertionError("valid cache should avoid invoking Git")

    monkeypatch.setattr(tracked.subprocess, "run", unavailable_git)

    assert tracked.tracked_files(repo, use_cache=True, update_cache=False) == expected


def test_cache_bypass_forces_fresh_git_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    tracked.tracked_files(repo)
    cache = repo / ".ai" / "cache" / "tracked-files.json"
    payload = json.loads(cache.read_text(encoding="utf-8"))
    payload["paths"] = []
    cache.write_text(json.dumps(payload), encoding="utf-8")
    if os.name != "nt":
        cache.chmod(0o600)
    real_run = subprocess.run
    calls = {"git": 0}

    def counting_git(*args, **kwargs):
        calls["git"] += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(tracked.subprocess, "run", counting_git)
    paths = tracked.tracked_files(repo, use_cache=False, update_cache=False)

    assert repo / "first.py" in paths
    assert calls["git"] == 1


@pytest.mark.skipif(os.name == "nt", reason="Unix cache trust boundary")
def test_symlinked_tracked_cache_is_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    cache = repo / ".ai" / "cache" / "tracked-files.json"
    cache.parent.mkdir(parents=True)
    sibling = repo.with_name(repo.name + "-cache-target.json")
    sibling.write_text('{"schema": 2, "paths": []}\n', encoding="utf-8")
    cache.symlink_to(sibling)
    real_run = subprocess.run
    calls = {"git": 0}

    def counting_git(*args, **kwargs):
        calls["git"] += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(tracked.subprocess, "run", counting_git)
    paths = tracked.tracked_files(repo)

    assert repo / "first.py" in paths
    assert calls["git"] == 1
    assert sibling.read_text(encoding="utf-8") == '{"schema": 2, "paths": []}\n'
    assert cache.is_file() and not cache.is_symlink()


@pytest.mark.skipif(os.name == "nt", reason="Unix cache mode")
def test_tracked_cache_file_is_private(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    tracked.tracked_files(repo)

    cache = repo / ".ai" / "cache" / "tracked-files.json"
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_hardlinked_tracked_cache_forces_git_refresh_without_modifying_external(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    expected = tracked.tracked_files(repo)
    cache = repo / ".ai" / "cache" / "tracked-files.json"
    content = cache.read_text(encoding="utf-8")
    cache.unlink()
    external = tmp_path / "external-tracked-cache.json"
    external.write_text(content, encoding="utf-8")
    if os.name != "nt":
        external.chmod(0o600)
    os.link(external, cache)
    real_run = subprocess.run
    calls = {"git": 0}

    def counting_git(*args, **kwargs):
        calls["git"] += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(tracked.subprocess, "run", counting_git)
    actual = tracked.tracked_files(repo)

    assert actual == expected
    assert calls["git"] == 1
    assert external.read_text(encoding="utf-8") == content
    assert cache.stat().st_ino != external.stat().st_ino


def test_filesystem_baseline_excludes_gitignored_runtime_scratch(tmp_path: Path) -> None:
    """A git-less workspace must not present runtime scratch as tracked source.

    Regression: `.ai/tmp` held an upstream third-party clone used for hook research.
    The release smoke copy strips `.git`, so the filesystem baseline walked that
    scratch tree and `secret_scan` flagged upstream test fixtures, failing strict
    doctor (exit 10) with findings absent from tracked source.
    """
    repo = tmp_path / "detached"
    (repo / ".ai" / "tmp" / "upstream-clone" / "core").mkdir(parents=True)
    (repo / ".ai" / "outputs").mkdir(parents=True)
    (repo / ".ai" / "runtime" / "src").mkdir(parents=True)
    (repo / "src").mkdir()

    real_source = repo / "src" / "app.py"
    real_source.write_text("value = 1\n", encoding="utf-8")
    runtime_source = repo / ".ai" / "runtime" / "src" / "core.py"
    runtime_source.write_text("value = 2\n", encoding="utf-8")
    scratch = repo / ".ai" / "tmp" / "upstream-clone" / "core" / "fixture.rs"
    # Built at runtime so this test file never contains a literal that the
    # secret scanner would flag in tracked source.
    scratch.write_text("let key = \"AKIA\" + \"IOSFODNN7EXAMPLE\";\n", encoding="utf-8")
    report = repo / ".ai" / "outputs" / "report.json"
    report.write_text("{}\n", encoding="utf-8")

    baseline = tracked.tracked_files(repo)

    assert baseline.source == "filesystem"
    assert real_source in baseline
    assert runtime_source in baseline, "product source under .ai/runtime stays in the baseline"
    assert scratch not in baseline
    assert report not in baseline


def test_filesystem_baseline_skip_prefixes_cover_search_scratch_roots() -> None:
    """Scratch roots excluded from indexing must also be excluded from the baseline."""
    from ai_core.search import SKIP_PATH_PREFIXES

    for prefix in SKIP_PATH_PREFIXES:
        assert prefix in tracked.FILESYSTEM_BASELINE_SKIP_PREFIXES, prefix
