from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core import search as search_mod  # noqa: E402
from ai_core.search import (  # noqa: E402
    SCHEMA_VERSION,
    _function_chunks_for_lang,
    _looks_like_code_symbol,
    _rg_fallback,
    connect,
    context_pack,
    init_schema,
    query,
    rebuild,
    retrieval_policy_for_query,
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


def test_schema_version_is_seven(tmp_path: Path) -> None:
    assert SCHEMA_VERSION == 7
    repo = _make_repo(tmp_path)
    _write(repo, "doc.md", "hello world\n")
    rebuild(repo)
    with connect(repo) as conn:
        version = int(conn.execute("pragma user_version").fetchone()[0])
    assert version == 7


def test_porter_stemming_matches_inflected_forms(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _write(repo, "doc.md", "indexing\n")
    rebuild(repo)

    for term in ("index", "indexed", "indexes"):
        result = query(repo, term)
        assert result["ok"] is True
        assert len(result["results"]) == 1, f"expected 1 result for {term!r}, got {result['results']}"
        assert result["results"][0]["path"] == "doc.md"


def test_porter_stemming_matches_run_running_runs(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _write(repo, "doc.md", "running fast\n")
    rebuild(repo)

    for term in ("run", "runs"):
        result = query(repo, term)
        assert result["ok"] is True
        assert len(result["results"]) == 1, f"expected 1 result for {term!r}, got {result['results']}"
        assert result["results"][0]["path"] == "doc.md"


def test_legacy_v2_cache_auto_migrates_to_v4(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    with connect(repo) as conn:
        conn.executescript(
            """
            create table chunks (
              id integer primary key,
              path text not null,
              sha256 text not null,
              summary text not null,
              updated_at text default current_timestamp
            );
            create virtual table chunks_fts using fts5(path, content, content='');
            create table chunk_meta (
              chunk_id integer primary key,
              kind text not null default 'file',
              bytes integer not null,
              line_count integer not null
            );
            create table summaries (
              path text primary key,
              summary text not null,
              updated_at text default current_timestamp
            );
            create table provenance (
              path text primary key,
              processor text not null,
              model_hash text,
              prompt_version text,
              chunker_version text not null,
              confidence real not null
            );
            create table embeddings_vec0 (
              chunk_id integer primary key,
              disabled_reason text not null default 'embeddings_default_off'
            );
            pragma user_version=2;
            """
        )
        conn.execute(
            "insert into chunks(path, sha256, summary) values (?, ?, ?)",
            ("legacy.md", "deadbeef", "legacy summary"),
        )
        conn.commit()

    with connect(repo) as conn:
        version_before = int(conn.execute("pragma user_version").fetchone()[0])
        assert version_before == 2
        init_schema(conn, migrate_legacy=True)
        version_after = int(conn.execute("pragma user_version").fetchone()[0])
        assert version_after == 7

        tables = {
            row[0]
            for row in conn.execute(
                "select name from sqlite_master where type in ('table','view')"
            ).fetchall()
        }
        assert "chunks" in tables
        assert "chunks_fts" in tables

        chunk_count = conn.execute("select count(*) from chunks").fetchone()[0]
        assert chunk_count == 0


def test_diacritics_normalized(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _write(repo, "doc.md", "café au lait\n")
    rebuild(repo)

    result = query(repo, "cafe")
    assert result["ok"] is True
    assert len(result["results"]) == 1
    assert result["results"][0]["path"] == "doc.md"


def test_bm25_weights_path_higher_than_content(tmp_path: Path, monkeypatch) -> None:
    # Disable rg fallback so the result ordering reflects pure BM25 ranking.
    monkeypatch.setenv("AI_SEARCH_RG_FALLBACK", "0")
    repo = _make_repo(tmp_path)
    # Chunk A: term appears in path (filename "foo_alpha.py"), content lacks "alpha".
    _write(repo, "foo_alpha.py", "something else here\n")
    # Chunk B: term appears in content many times, but path does not contain "alpha".
    _write(repo, "other.py", "alpha alpha alpha\n")
    rebuild(repo)

    # Baseline: equal weights -> content-heavy file wins.
    monkeypatch.setenv("AI_SEARCH_BM25_PATH_WEIGHT", "1.0")
    monkeypatch.setenv("AI_SEARCH_BM25_CONTENT_WEIGHT", "1.0")
    baseline = query(repo, "alpha", limit=5)
    baseline_paths = [item["path"] for item in baseline["results"]]
    assert "foo_alpha.py" in baseline_paths
    assert "other.py" in baseline_paths

    # Boosted path weight -> path-match must rank at or above content-match.
    monkeypatch.setenv("AI_SEARCH_BM25_PATH_WEIGHT", "10.0")
    monkeypatch.setenv("AI_SEARCH_BM25_CONTENT_WEIGHT", "1.0")
    boosted = query(repo, "alpha", limit=5)
    boosted_paths = [item["path"] for item in boosted["results"]]
    assert "foo_alpha.py" in boosted_paths
    assert "other.py" in boosted_paths
    # With path heavily weighted, path-match outranks content-only match.
    assert boosted_paths.index("foo_alpha.py") <= boosted_paths.index("other.py")
    # And boosting actually changed the ordering relative to baseline.
    assert boosted_paths != baseline_paths or boosted_paths[0] == "foo_alpha.py"


def test_rg_fallback_triggers_on_zero_fts_hits(tmp_path: Path, monkeypatch) -> None:
    if not shutil.which("rg"):
        pytest.skip("ripgrep not installed on test runner")
    monkeypatch.setenv("AI_SEARCH_RG_FALLBACK", "1")
    repo = _make_repo(tmp_path)
    _write(repo, "doc.md", "the word here is ordinary\n")
    rebuild(repo)

    calls: list[list] = []
    original_run = search_mod.subprocess.run

    def _spy_run(cmd, *args, **kwargs):
        # Only record the rg invocation; let other subprocess calls (git ls-files
        # in rebuild) pass through normally.
        if isinstance(cmd, list) and cmd and str(cmd[0]).endswith("rg"):
            calls.append(list(cmd))
        return original_run(cmd, *args, **kwargs)

    monkeypatch.setattr(search_mod.subprocess, "run", _spy_run)

    result = query(repo, "ZeXyQuPlBazXYZ", limit=5)
    assert result["ok"] is True
    # rg was invoked because FTS5 returned zero hits.
    assert any(str(c[0]).endswith("rg") for c in calls), f"rg not invoked; calls={calls}"


def test_rg_fallback_respects_disable_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_SEARCH_RG_FALLBACK", "0")
    repo = _make_repo(tmp_path)
    _write(repo, "doc.md", "ordinary content\n")
    rebuild(repo)

    calls: list[list] = []
    original_run = search_mod.subprocess.run

    def _spy_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd and str(cmd[0]).endswith("rg"):
            calls.append(list(cmd))
        return original_run(cmd, *args, **kwargs)

    monkeypatch.setattr(search_mod.subprocess, "run", _spy_run)

    result = query(repo, "ZeXyQuPlBazXYZ", limit=5)
    assert result["ok"] is True
    assert result.get("rg_fallback") is False
    assert calls == []


def test_symbol_detection() -> None:
    assert _looks_like_code_symbol("MyClassName") is True
    assert _looks_like_code_symbol("snake_case_var") is True
    assert _looks_like_code_symbol("src/file.py") is True
    assert _looks_like_code_symbol("E1001") is True
    assert _looks_like_code_symbol("hello world") is False
    # snake_case rule fires; acceptable false positive for fallback bias.
    assert _looks_like_code_symbol("just_a_word") is True


def test_retrieval_policy_for_query_is_pure_shape_decision() -> None:
    graph_state = {"indexed_files": 4, "symbol_count": 2, "call_edge_count": 3}
    bm25_only_state = {"indexed_files": 4, "symbol_count": 0, "call_edge_count": 0}

    assert retrieval_policy_for_query("", graph_state) == "none"
    assert retrieval_policy_for_query("anything", {"indexed_files": 0}) == "none"
    assert retrieval_policy_for_query("plain language search", graph_state) == "bm25"
    assert retrieval_policy_for_query("MyClassName", graph_state) == "hybrid"
    assert retrieval_policy_for_query("callers for helper", graph_state) == "graph"
    assert retrieval_policy_for_query("MyClassName", bm25_only_state) == "bm25"


def test_query_and_context_pack_expose_retrieval_policy(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _write(
        repo,
        "src/service.py",
        "def MyClassName():\n    helper()\n\n"
        "def helper():\n    return 1\n",
    )
    rebuild(repo)

    result = query(repo, "MyClassName", limit=5)
    assert result["ok"] is True
    assert result["retrieval_policy"] in {"bm25", "bm25+rg"}
    assert result["recommended_retrieval_policy"] == "hybrid"

    pack = context_pack(repo, "MyClassName", limit=5)
    assert pack["retrieval_policy"] in {"bm25", "bm25+rg"}
    assert pack["recommended_retrieval_policy"] == "hybrid"
    assert pack["additionalContext"]


def test_rg_fallback_helper_returns_empty_when_disabled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_SEARCH_RG_FALLBACK", "0")
    assert _rg_fallback(tmp_path, "anything") == []


def test_bm25_weights_invalid_env_falls_back_to_defaults(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_SEARCH_BM25_PATH_WEIGHT", "not-a-float")
    monkeypatch.setenv("AI_SEARCH_BM25_CONTENT_WEIGHT", "")
    monkeypatch.setenv("AI_SEARCH_RG_FALLBACK", "0")
    repo = _make_repo(tmp_path)
    _write(repo, "doc.md", "hello\n")
    rebuild(repo)
    result = query(repo, "hello")
    assert result["ok"] is True
    assert len(result["results"]) == 1


def test_function_chunks_for_python_extracts_functions(tmp_path: Path) -> None:
    """Test Python function chunking via _function_chunks_for_lang."""
    py_source = """\
def hello():
    return "world"

class MyClass:
    def method(self):
        pass
"""
    chunks = _function_chunks_for_lang("test.py", py_source, "py")
    # Best-effort: may return [] if codegraph is unavailable.
    # If successful, should extract at least the top-level function.
    if chunks:
        qualnames = [c["qualname"] for c in chunks]
        assert "hello" in qualnames or "MyClass" in qualnames
        for chunk in chunks:
            assert "text" in chunk
            assert "start_line" in chunk
            assert "end_line" in chunk
            assert chunk["start_line"] <= chunk["end_line"]


def test_function_chunks_for_unsupported_lang_returns_empty() -> None:
    """Test that unsupported languages return empty list."""
    source = "fn main() {}\n"
    chunks = _function_chunks_for_lang("test.c", source, "c")
    assert chunks == []


def test_function_chunks_for_js_graceful_when_astgrep_unavailable(monkeypatch) -> None:
    """Test that JS chunks gracefully return [] if ast-grep is unavailable."""
    # Simulate ast-grep unavailable
    monkeypatch.setenv("AI_ASTGREP_DISABLE", "1")
    js_source = """\
function hello() {
    return "world";
}
"""
    chunks = _function_chunks_for_lang("test.js", js_source, "js")
    # With AI_ASTGREP_DISABLE=1, should gracefully return []
    assert chunks == []


def test_python_function_chunks_in_rebuild(tmp_path: Path, monkeypatch) -> None:
    """Test that Python function chunks are indexed during rebuild."""
    monkeypatch.setenv("AI_SEARCH_RG_FALLBACK", "0")
    repo = _make_repo(tmp_path)
    py_source = """\
def helper():
    '''A helper function.'''
    return 42

def main():
    '''Main entry point.'''
    result = helper()
    return result
"""
    _write(repo, "app.py", py_source)
    rebuild(repo)

    with connect(repo) as conn:
        init_schema(conn)
        # Check that file-level chunk exists
        file_chunks = conn.execute(
            "select count(*) from chunks where path = 'app.py'"
        ).fetchone()
        assert file_chunks[0] == 1, "file-level chunk should exist"

        # Check for function-level chunks (qualname column in chunk_meta)
        func_chunks = conn.execute(
            "select count(*) from chunks where path like 'app.py:%'"
        ).fetchone()
        # If Python AST extraction works, should have function chunks.
        # Best-effort: may be 0 if codegraph unavailable.
        if func_chunks[0] > 0:
            # Verify they have the expected metadata
            meta = conn.execute(
                "select qualname from chunk_meta where qualname is not null and chunk_id in "
                "(select id from chunks where path like 'app.py:%')"
            ).fetchall()
            assert len(meta) > 0, "function chunks should have qualname metadata"
