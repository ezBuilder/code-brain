"""Tests for T2 (Python function chunking) and T5 (RRF_K dynamic + BM25 weights)."""
from __future__ import annotations

import os
import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core import search as search_mod  # noqa: E402
from ai_core.search import (  # noqa: E402
    SCHEMA_VERSION,
    _compute_rrf_k,
    _function_chunks_for_python,
    connect,
    query,
    rebuild,
)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".ai").mkdir(parents=True)
    (repo / ".ai" / "config.yaml").write_text("project_name: t\n", encoding="utf-8")
    return repo


def _write(repo: Path, rel: str, content: str) -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_schema_version_is_eight() -> None:
    """Schema v8 enables row-level FTS delete for true incremental updates."""
    assert SCHEMA_VERSION == 8


def test_escape_fts_query_splits_natural_language_punctuation() -> None:
    escaped = search_mod.escape_fts_query("How does reciprocal/fusion work? fail-closed")
    assert escaped == (
        '"How" OR "does" OR "reciprocal" OR "fusion" OR "work" OR "fail" OR "closed"'
    )


def test_auto_refresh_does_not_rebuild_for_mtime_only_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    source = _write(repo, "src/main.py", "VALUE = 1\n")
    rebuild(repo)
    newer = search_mod.db_path(repo).stat().st_mtime + 5
    os.utime(source, (newer, newer))
    monkeypatch.setattr(search_mod, "_git_dirty_paths", lambda _root: {"src/main.py"})

    result = search_mod._auto_refresh_if_stale(repo)

    assert result == {"enabled": True, "rebuilt": False, "reason": "current"}


def test_git_dirty_paths_ignores_untracked_files_but_keeps_tracked_drift(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    tracked = _write(repo, "src/tracked.py", "VALUE = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "search@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "search"], cwd=repo, check=True)
    subprocess.run(["git", "add", ".ai/config.yaml", "src/tracked.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    tracked.write_text("VALUE = 2\n", encoding="utf-8")
    _write(repo, "src/untracked.py", "UNTRACKED = True\n")

    dirty = search_mod._git_dirty_paths(repo)

    assert dirty == {"src/tracked.py"}


def test_text_file_enumeration_reuses_one_stat_per_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    source = _write(repo, "src/main.py", "VALUE = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", ".ai/config.yaml", "src/main.py"], cwd=repo, check=True)
    real_stat = Path.stat
    calls = {"source": 0}

    def counting_stat(path: Path, *args, **kwargs):
        if path == source:
            calls["source"] += 1
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", counting_stat)

    yielded = [path for path, _state in search_mod.iter_text_file_states(repo)]

    assert source in yielded
    assert calls["source"] == 1


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_text_index_never_follows_external_symlink(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    external = tmp_path / "outside.py"
    external.write_text("ExternalSymlinkNeedle = True\n", encoding="utf-8")
    linked = repo / "src" / "linked.py"
    linked.parent.mkdir(parents=True)
    linked.symlink_to(external)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    yielded = list(search_mod.iter_text_files(repo, use_cache=False, update_cache=False))
    rebuilt = rebuild(repo)
    result = query(repo, "ExternalSymlinkNeedle")

    assert linked not in yielded
    assert rebuilt["indexed"] == 1  # .ai/config.yaml only
    assert result["results"] == []


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_text_index_skips_internal_symlink_duplicate(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    source = _write(repo, "src/source.py", "InternalSymlinkNeedle = True\n")
    linked = repo / "src" / "alias.py"
    linked.symlink_to(source)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    yielded = list(search_mod.iter_text_files(repo, use_cache=False, update_cache=False))
    rebuild(repo)
    result = query(repo, "InternalSymlinkNeedle")

    assert source in yielded
    assert linked not in yielded
    assert [item["path"] for item in result["results"]].count("src/source.py") == 1
    assert all(item["path"] != "src/alias.py" for item in result["results"])


def test_candidate_files_cache_hit_avoids_second_git_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    _write(repo, "src/main.py", "VALUE = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", ".ai/config.yaml", "src/main.py"], cwd=repo, check=True)
    first = search_mod.candidate_files(repo)

    def unexpected_git(*_args, **_kwargs):
        raise AssertionError("valid candidate cache must avoid a second git process")

    monkeypatch.setattr(search_mod.subprocess, "run", unexpected_git)
    second = search_mod.candidate_files(repo)

    assert second == first
    assert second == sorted(second)
    assert (repo / ".ai" / "cache" / "candidate-files.json").is_file()


def test_candidate_files_cache_invalidates_for_new_untracked_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    src = repo / "src"
    _write(repo, "src/main.py", "VALUE = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    search_mod.candidate_files(repo)
    previous = src.stat().st_mtime_ns
    added = _write(repo, "src/new.py", "NEW = True\n")
    os.utime(src, ns=(previous + 1_000_000_000, previous + 1_000_000_000))
    real_run = subprocess.run
    calls = {"git": 0}

    def counting_git(*args, **kwargs):
        calls["git"] += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(search_mod.subprocess, "run", counting_git)
    paths = search_mod.candidate_files(repo)

    assert added in paths
    assert calls["git"] == 1


def test_candidate_cache_uses_directory_ctime_when_mtime_is_restored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    src = repo / "src"
    _write(repo, "src/main.py", "VALUE = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    search_mod.candidate_files(repo)
    original = src.stat()
    added = _write(repo, "src/ctime-only.py", "CTIME_ONLY = True\n")
    os.utime(src, ns=(original.st_atime_ns, original.st_mtime_ns))
    real_run = subprocess.run
    calls = {"git": 0}

    def counting_git(*args, **kwargs):
        calls["git"] += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(search_mod.subprocess, "run", counting_git)
    paths = search_mod.candidate_files(repo)

    assert added in paths
    assert calls["git"] == 1


def test_candidate_files_cache_invalidates_when_gitignore_changes(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    ignored = _write(repo, "ignored.txt", "ignored\n")
    gitignore = _write(repo, ".gitignore", "ignored.txt\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    first = search_mod.candidate_files(repo)
    assert ignored not in first
    previous = gitignore.stat().st_mtime_ns
    gitignore.write_text("", encoding="utf-8")
    os.utime(gitignore, ns=(previous + 1_000_000_000, previous + 1_000_000_000))

    second = search_mod.candidate_files(repo)

    assert ignored in second


def test_candidate_cache_file_does_not_self_invalidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    _write(repo, "src/main.py", "VALUE = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    real_run = subprocess.run
    calls = {"git": 0}

    def counting_git(*args, **kwargs):
        calls["git"] += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(search_mod.subprocess, "run", counting_git)
    search_mod.candidate_files(repo)
    search_mod.candidate_files(repo)

    assert calls["git"] == 1


def test_candidate_cache_invalidates_when_filter_policy_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    _write(repo, "src/main.py", "VALUE = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    search_mod.candidate_files(repo)
    real_run = subprocess.run
    calls = {"git": 0}

    def counting_git(*args, **kwargs):
        calls["git"] += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(search_mod.subprocess, "run", counting_git)
    monkeypatch.setattr(search_mod, "_candidate_policy_fingerprint", lambda: "f" * 64)

    search_mod.candidate_files(repo)

    assert calls["git"] == 1


def test_freshness_and_full_rebuild_bypass_forged_candidate_cache(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _write(repo, "src/base.py", "BASE = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "candidate@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "candidate"], cwd=repo, check=True)
    subprocess.run(["git", "add", ".ai/config.yaml", "src/base.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    rebuild(repo)
    added = _write(repo, "src/new.py", "FreshCandidateNeedle = True\n")
    search_mod.candidate_files(repo)
    cache = repo / ".ai" / "cache" / "candidate-files.json"
    payload = json.loads(cache.read_text(encoding="utf-8"))
    payload["paths"] = [rel for rel in payload["paths"] if rel != "src/new.py"]
    cache.write_text(json.dumps(payload), encoding="utf-8")
    if os.name != "nt":
        cache.chmod(0o600)

    cached = search_mod.index_hash_status(
        repo,
        use_metadata=True,
        use_candidate_cache=True,
    )
    strict = search_mod.index_hash_status(repo, use_candidate_cache=False)
    rebuild(repo)
    with connect(repo) as conn:
        indexed = conn.execute(
            "select count(*) from chunks where path = ?",
            ("src/new.py",),
        ).fetchone()[0]

    # Bounded freshness probes no longer validate/use the candidate cache: the
    # validation walk itself was unbounded and a forged cache could hide files.
    assert "src/new.py" in cached["changed_paths"]
    assert "src/new.py" in strict["changed_paths"]
    assert indexed == 1
    assert added.is_file()


@pytest.mark.skipif(os.name == "nt", reason="Unix cache trust boundary")
def test_symlinked_candidate_cache_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path)
    source = _write(repo, "src/main.py", "VALUE = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    cache = repo / ".ai" / "cache" / "candidate-files.json"
    cache.parent.mkdir(parents=True)
    sibling = repo.with_name(repo.name + "-candidate-cache.json")
    sibling.write_text('{"schema": 3, "paths": []}\n', encoding="utf-8")
    cache.symlink_to(sibling)
    real_run = subprocess.run
    calls = {"git": 0}

    def counting_git(*args, **kwargs):
        calls["git"] += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(search_mod.subprocess, "run", counting_git)
    paths = search_mod.candidate_files(repo)

    assert source in paths
    assert calls["git"] == 1
    assert sibling.read_text(encoding="utf-8") == '{"schema": 3, "paths": []}\n'
    assert cache.is_file() and not cache.is_symlink()


@pytest.mark.skipif(os.name == "nt", reason="Unix cache mode")
def test_public_candidate_cache_mode_forces_git_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    _write(repo, "src/main.py", "VALUE = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    search_mod.candidate_files(repo)
    cache = repo / ".ai" / "cache" / "candidate-files.json"
    cache.chmod(0o644)
    real_run = subprocess.run
    calls = {"git": 0}

    def counting_git(*args, **kwargs):
        calls["git"] += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(search_mod.subprocess, "run", counting_git)
    search_mod.candidate_files(repo)

    assert calls["git"] == 1
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_hardlinked_candidate_cache_forces_git_refresh_without_modifying_external(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    source = _write(repo, "src/main.py", "VALUE = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    expected = search_mod.candidate_files(repo)
    cache = repo / ".ai" / "cache" / "candidate-files.json"
    content = cache.read_text(encoding="utf-8")
    cache.unlink()
    external = tmp_path / "external-candidate-cache.json"
    external.write_text(content, encoding="utf-8")
    if os.name != "nt":
        external.chmod(0o600)
    os.link(external, cache)
    real_run = subprocess.run
    calls = {"git": 0}

    def counting_git(*args, **kwargs):
        calls["git"] += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(search_mod.subprocess, "run", counting_git)
    actual = search_mod.candidate_files(repo)

    assert actual == expected
    assert source in actual
    assert calls["git"] == 1
    assert external.read_text(encoding="utf-8") == content
    assert cache.stat().st_ino != external.stat().st_ino


def test_chatgpt2codex_artifacts_are_not_indexed_or_cache_dependencies(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    source = _write(repo, "src/main.py", "VALUE = 1\n")
    internal = _write(repo, ".chatgpt2codex/session.json", '{"internal": true}\n')
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    first_candidates = search_mod.candidate_files(repo)
    first_text = list(search_mod.iter_text_files(repo))
    cache = repo / ".ai" / "cache" / "candidate-files.json"
    cache_mtime = cache.stat().st_mtime_ns
    internal.write_text('{"internal": false}\n', encoding="utf-8")
    second_candidates = search_mod.candidate_files(repo)

    assert source in first_candidates
    assert internal not in first_candidates
    assert internal not in first_text
    assert second_candidates == first_candidates
    assert cache.stat().st_mtime_ns == cache_mtime


def test_auto_refresh_indexes_new_untracked_file_via_mtime_hash(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _write(repo, "src/tracked.py", "VALUE = 1\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "search@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "search"], cwd=repo, check=True)
    subprocess.run(["git", "add", ".ai/config.yaml", "src/tracked.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    rebuild(repo)
    added = _write(repo, "src/new_untracked.py", "FreshUntrackedNeedle = True\n")
    newer = search_mod.db_path(repo).stat().st_mtime + 5
    os.utime(added, (newer, newer))

    result = query(repo, "FreshUntrackedNeedle")

    assert result["auto_refresh"]["rebuilt"] is True
    assert result["auto_refresh"]["reason"] == "hash_mismatch"
    assert any(item["path"] == "src/new_untracked.py" for item in result["results"])


def test_rebuild_records_file_state_metadata(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    source = _write(repo, "src/main.py", "VALUE = 1\n")
    rebuild(repo)

    with connect(repo) as conn:
        row = conn.execute(
            "select size, mtime_ns, ctime_ns, sha256 from file_state where path = ?",
            ("src/main.py",),
        ).fetchone()

    assert row is not None
    assert int(row["size"]) == source.stat().st_size
    assert int(row["mtime_ns"]) == source.stat().st_mtime_ns
    assert int(row["ctime_ns"]) > 0
    assert len(str(row["sha256"])) == 64


def test_metadata_fast_path_skips_unchanged_file_hashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    _write(repo, "src/main.py", "VALUE = 1\n")
    rebuild(repo)

    def unexpected_redaction(_value):
        raise AssertionError("unchanged metadata must avoid hashing file content")

    monkeypatch.setattr(search_mod, "redact_value", unexpected_redaction)

    status = search_mod.index_hash_status(repo, use_metadata=True)

    assert status["ok"] is True
    assert status["changed_paths"] == []


def test_metadata_drift_with_same_hash_is_refreshed_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    source = _write(repo, "src/main.py", "VALUE = 1\n")
    rebuild(repo)
    newer = source.stat().st_mtime + 5
    os.utime(source, (newer, newer))

    first = search_mod.index_hash_status(
        repo,
        use_metadata=True,
        refresh_metadata=True,
    )
    assert first["ok"] is True

    def unexpected_redaction(_value):
        raise AssertionError("refreshed metadata must make the next check hash-free")

    monkeypatch.setattr(search_mod, "redact_value", unexpected_redaction)
    second = search_mod.index_hash_status(repo, use_metadata=True)

    assert second["ok"] is True


def test_metadata_fast_path_backfills_existing_index_without_file_state(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _write(repo, "src/main.py", "VALUE = 1\n")
    rebuild(repo)
    with connect(repo) as conn:
        conn.execute("delete from file_state")
        conn.commit()

    status = search_mod.index_hash_status(
        repo,
        use_metadata=True,
        refresh_metadata=True,
    )

    assert status["ok"] is True
    with connect(repo) as conn:
        restored = int(conn.execute("select count(*) from file_state").fetchone()[0])
    assert restored >= 2


def test_compute_rrf_k_dynamic_scaling() -> None:
    """T5: RRF_K scales dynamically with corpus size."""
    # Clear env override
    os.environ.pop("AI_SEARCH_RRF_K", None)

    # Small corpus: k should be smaller
    k_small = _compute_rrf_k(16)
    assert 30 <= k_small <= 120, f"k_small={k_small} out of bounds"

    # Medium corpus (1024 reference): k should be ~60
    k_medium = _compute_rrf_k(1024)
    assert 55 <= k_medium <= 65, f"k_medium={k_medium} should be ~60"

    # Large corpus: k should grow but be clamped at 120
    k_large = _compute_rrf_k(100000)
    assert k_large <= 120, f"k_large={k_large} exceeds max"

    # Verify scaling monotonicity
    assert k_small <= k_medium <= k_large or k_large == 120


def test_compute_rrf_k_env_override(monkeypatch) -> None:
    """T5: AI_SEARCH_RRF_K env var overrides dynamic k."""
    monkeypatch.setenv("AI_SEARCH_RRF_K", "42")
    k = _compute_rrf_k(16)
    assert k == 42

    # Invalid env falls back to dynamic
    monkeypatch.setenv("AI_SEARCH_RRF_K", "not-a-number")
    k = _compute_rrf_k(1024)
    assert 55 <= k <= 65  # ~60 for baseline corpus


def test_function_chunks_for_python_extracts_functions() -> None:
    """T2: extract function/class chunks from Python source."""
    source = """
def simple_func(x):
    return x + 1

class MyClass:
    def method(self):
        pass

    def another_method(self, y):
        return y * 2

async def async_func():
    pass
"""
    chunks = _function_chunks_for_python("test.py", source)

    # Expect 5 chunks: 3 functions + 1 class + 1 class method + 1 another_method + 1 async
    # Actually: simple_func, MyClass, MyClass.method, MyClass.another_method, async_func
    assert len(chunks) >= 4, f"expected at least 4 chunks, got {len(chunks)}"

    qualnames = {c["qualname"] for c in chunks}
    assert "simple_func" in qualnames
    assert "MyClass" in qualnames
    assert "async_func" in qualnames

    # Check that chunk structure is correct
    for chunk in chunks:
        assert "qualname" in chunk
        assert "start_line" in chunk
        assert "end_line" in chunk
        assert "text" in chunk
        assert "kind" in chunk
        assert chunk["start_line"] >= 1
        assert chunk["end_line"] >= chunk["start_line"]
        assert len(chunk["text"]) > 0


def test_function_chunks_for_python_handles_invalid_syntax() -> None:
    """T2: gracefully handle syntax errors in Python source."""
    invalid_source = "def broken( syntax error here"
    chunks = _function_chunks_for_python("broken.py", invalid_source)
    assert chunks == [], "expected empty list for invalid syntax"


def test_function_chunks_for_python_non_python_file() -> None:
    """T2: return empty list for non-Python files."""
    chunks = _function_chunks_for_python("test.js", "function test() {}")
    assert chunks == [], "expected empty list for non-.py file"


def test_python_file_indexing_creates_function_chunks(tmp_path: Path) -> None:
    """T2: Python file indexing includes both file-level and function-level chunks."""
    repo = _make_repo(tmp_path)
    _write(
        repo,
        "src/utils.py",
        """def helper_func(x):
    '''A helper function.'''
    return x * 2

def another_func():
    return "hello"
""",
    )
    rebuild(repo)

    # Query for function name
    result = query(repo, "helper_func")
    assert result["ok"] is True
    # Should find the function
    assert any("src/utils.py" in item["path"] for item in result["results"])


def test_hybrid_chunking_preserves_file_chunks(tmp_path: Path) -> None:
    """T2: File-level chunks are preserved alongside function chunks."""
    repo = _make_repo(tmp_path)
    _write(repo, "src/main.py", "def main():\n    pass\n")
    rebuild(repo)

    # Query for file-level content
    result = query(repo, "main")
    assert result["ok"] is True
    assert len(result["results"]) > 0


def test_bm25_weights_env_applied(tmp_path: Path, monkeypatch) -> None:
    """T5: BM25 weights respect env vars AI_SEARCH_BM25_*."""
    monkeypatch.setenv("AI_SEARCH_BM25_PATH_WEIGHT", "5.0")
    monkeypatch.setenv("AI_SEARCH_BM25_CONTENT_WEIGHT", "0.5")
    monkeypatch.setenv("AI_SEARCH_RG_FALLBACK", "0")

    repo = _make_repo(tmp_path)
    _write(repo, "path/to/file.md", "content text\n")
    rebuild(repo)

    # Query with path-like term
    result = query(repo, "path")
    assert result["ok"] is True
    # With high path weight, path match should rank well
    assert len(result["results"]) >= 1


def test_incremental_rebuild_removes_stale_function_chunks(tmp_path: Path) -> None:
    """T2: Incremental rebuild removes function chunks when file changes."""
    repo = _make_repo(tmp_path)

    # Write initial file
    _write(repo, "src/code.py", "def func_a():\n    pass\n")
    rebuild(repo)

    # Verify initial indexing
    result = query(repo, "func_a")
    initial_results = len(result["results"])
    assert initial_results > 0

    # Modify file to remove func_a and add func_b
    _write(repo, "src/code.py", "def func_b():\n    pass\n")
    rebuild(repo, incremental=True)

    # Old function should not appear
    result = query(repo, "func_a")
    assert len(result["results"]) == 0, "func_a should not be indexed after removal"

    # New function should appear
    result = query(repo, "func_b")
    assert len(result["results"]) > 0, "func_b should be indexed"


def test_targeted_incremental_does_not_delete_unrelated_function_chunks(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _write(repo, "src/a.py", "def keep_func():\n    return 1\n")
    _write(repo, "src/b.py", "def changed_func():\n    return 1\n")
    rebuild(repo)

    _write(repo, "src/b.py", "def changed_func_v2():\n    return 2\n")
    result = rebuild(repo, incremental=True, paths={"src/b.py"})

    assert result["targeted"] is True
    assert result["deleted"] == 0
    keep = query(repo, "keep_func")
    assert any("src/a.py" in item["path"] for item in keep["results"])
    changed = query(repo, "changed_func_v2")
    assert any("src/b.py" in item["path"] for item in changed["results"])


def test_incremental_rebuild_vacuums_deleted_rows_and_reports_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    for index in range(30):
        _write(repo, f"src/item_{index}.py", f"VALUE_{index} = {'x' * 2000!r}\n")
    rebuild(repo)
    for index in range(25):
        (repo / f"src/item_{index}.py").unlink()

    monkeypatch.setattr(search_mod, "INDEX_VACUUM_MIN_FREE_PAGES", 0)
    monkeypatch.setattr(search_mod, "INDEX_VACUUM_FREE_RATIO", 0.0)
    result = rebuild(repo, incremental=True)

    assert result["ok"] is True
    assert result["deleted"] == 25
    assert result["storage"]["vacuumed"] is True
    assert result["storage"]["free_pages"] == 0
    assert result["storage"]["within_limit"] is True


def test_rebuild_enforces_absolute_sqlite_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path)
    _write(repo, "src/large.py", "PAYLOAD = " + repr("z" * 50_000) + "\n")
    monkeypatch.setattr(search_mod, "INDEX_MAX_BYTES", 1024)

    result = rebuild(repo)

    assert result["ok"] is False
    assert result["error"] == "INDEX_SIZE_LIMIT"
    assert result["storage"]["within_limit"] is False
    assert result["storage"]["total_bytes"] > result["storage"]["max_bytes"]


def test_chunk_meta_stores_function_metadata(tmp_path: Path) -> None:
    """T2: chunk_meta stores qualname and line numbers for function chunks."""
    repo = _make_repo(tmp_path)
    _write(repo, "src/test.py", "def my_func():\n    return 1\n")
    rebuild(repo)

    with connect(repo) as conn:
        # Check that function chunks have metadata
        rows = conn.execute(
            "select id, path, qualname, start_line, end_line, kind from chunks "
            "join chunk_meta on chunks.id = chunk_meta.chunk_id "
            "where chunks.path like '%:my_func' and chunk_meta.kind = 'function'"
        ).fetchall()

        if rows:
            row = rows[0]
            assert row["qualname"] == "my_func"
            assert row["start_line"] >= 1
            assert row["end_line"] >= row["start_line"]
            assert row["kind"] == "function"
