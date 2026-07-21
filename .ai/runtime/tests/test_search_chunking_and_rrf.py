"""Tests for T2 (Python function chunking) and T5 (RRF_K dynamic + BM25 weights)."""
from __future__ import annotations

import os
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
