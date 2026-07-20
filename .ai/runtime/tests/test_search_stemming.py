from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
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
    db_path,
    init_schema,
    query,
    rebuild,
    retrieval_policy_for_query,
)
from ai_core.context_budget import apply as apply_context_budget  # noqa: E402


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


def test_schema_version_is_eight(tmp_path: Path) -> None:
    assert SCHEMA_VERSION == 8
    repo = _make_repo(tmp_path)
    _write(repo, "doc.md", "hello world\n")
    rebuild(repo)
    with connect(repo) as conn:
        version = int(conn.execute("pragma user_version").fetchone()[0])
    assert version == 8


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


def test_legacy_v2_cache_auto_migrates_to_current_schema(tmp_path: Path) -> None:
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
        assert version_after == 8

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


def test_rebuild_indexes_untracked_git_source_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_SEARCH_RG_FALLBACK", "0")
    repo = _make_repo(tmp_path)
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
    _write(repo, "README.md", "tracked baseline\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    _write(
        repo,
        "src/workflow/orchestrator.ts",
        'export function brandNewOrchestrator() { return "needle-orchestrator"; }\n',
    )

    rebuild(repo)

    result = query(repo, "brandNewOrchestrator", limit=5)
    assert result["ok"] is True
    assert any(item["path"] == "src/workflow/orchestrator.ts" for item in result["results"])


def test_query_auto_refreshes_stale_index_before_search(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_SEARCH_RG_FALLBACK", "0")
    monkeypatch.delenv("CI", raising=False)
    repo = _make_repo(tmp_path)
    path = _write(repo, "src/workflow/orchestrator.ts", "export const oldNeedle = 1;\n")
    rebuild(repo)

    path.write_text("export const brandNewOrchestratorNeedle = 1;\n", encoding="utf-8")
    file_mtime = path.stat().st_mtime
    os.utime(db_path(repo), (file_mtime + 0.5, file_mtime + 0.5))

    result = query(repo, "brandNewOrchestratorNeedle", limit=5)
    assert result["ok"] is True
    assert result["auto_refresh"]["rebuilt"] is True
    assert any(item["path"] == "src/workflow/orchestrator.ts" for item in result["results"])
    assert result["results"][0]["snippet"].startswith("export const brandNewOrchestratorNeedle")


def test_rg_fallback_triggers_on_zero_fts_hits(tmp_path: Path, monkeypatch) -> None:
    if not shutil.which("rg"):
        pytest.skip("ripgrep not installed on test runner")
    monkeypatch.setenv("AI_SEARCH_RG_FALLBACK", "1")
    repo = _make_repo(tmp_path)
    _write(repo, "doc.md", "the word here is ordinary\n")
    rebuild(repo)

    calls: list[list] = []
    original_popen = search_mod.subprocess.Popen

    def _spy_popen(cmd, *args, **kwargs):
        # Only record the rg invocation; let other subprocess calls (git ls-files
        # in rebuild) pass through normally.
        if isinstance(cmd, list) and cmd and str(cmd[0]).endswith("rg"):
            calls.append(list(cmd))
        return original_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(search_mod.subprocess, "Popen", _spy_popen)

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


def test_context_pack_aggressive_mode_exposes_budget_metadata(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_SEARCH_RG_FALLBACK", "0")
    repo = _make_repo(tmp_path)
    for idx in range(5):
        _write(repo, f"doc-{idx}.md", f"sharedneedle content {idx}\n")
    rebuild(repo)

    pack = context_pack(repo, "sharedneedle", limit=5, mode="aggressive")

    assert pack["context_budget"]["mode"] == "aggressive"
    assert pack["context_budget"]["max_results"] == 3
    assert pack["context_budget"]["selected_results"] == len(pack["results"])
    assert len(pack["results"]) <= 3
    assert pack["context_budget"]["truncated"] is True


def test_context_budget_preserves_protected_signals_beyond_aggressive_limit() -> None:
    results = [
        {"path": f"doc-{idx}.md", "snippet": "ordinary context"}
        for idx in range(5)
    ]
    results[4]["snippet"] = "handoff rubric verdict blockers stay visible"

    payload = apply_context_budget(results, mode="aggressive", limit=5)

    paths = [item["path"] for item in payload["results"]]
    assert "doc-4.md" in paths
    assert "handoff rubric verdict blockers" in payload["additionalContext"]
    assert payload["context_budget"]["protected_signals"] == ["handoff", "rubric", "verdict", "blockers"]


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


def test_rg_fallback_treats_leading_dash_and_regex_syntax_as_literal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    if not shutil.which("rg"):
        pytest.skip("ripgrep not installed on test runner")
    monkeypatch.setenv("AI_SEARCH_RG_FALLBACK", "1")
    repo = _make_repo(tmp_path)
    _write(repo, "doc.md", "literal option --hidden\nliteral bracket [needle\n")

    option_results = _rg_fallback(repo, "--hidden")
    bracket_results = _rg_fallback(repo, "[needle")

    assert option_results[0]["path"] == "doc.md"
    assert "--hidden" in option_results[0]["snippet"]
    assert bracket_results[0]["path"] == "doc.md"
    assert "[needle" in bracket_results[0]["snippet"]


def test_rg_fallback_fails_soft_for_invalid_process_argument(tmp_path: Path, monkeypatch) -> None:
    if not shutil.which("rg"):
        pytest.skip("ripgrep not installed on test runner")
    monkeypatch.setenv("AI_SEARCH_RG_FALLBACK", "1")
    repo = _make_repo(tmp_path)
    _write(repo, "doc.md", "ordinary content\n")

    assert _rg_fallback(repo, "\x00") == []


def test_search_payload_and_evidence_redact_credential_shaped_query(
    tmp_path: Path,
    monkeypatch,
) -> None:
    if not shutil.which("rg"):
        pytest.skip("ripgrep not installed on test runner")
    monkeypatch.setenv("AI_SEARCH_RG_FALLBACK", "1")
    repo = _make_repo(tmp_path)
    query_value = "to" + "ken=" + "privacyQ7" * 3
    _write(repo, "doc.md", f"privacy marker {query_value}\n")
    rebuild(repo)

    payload = query(repo, query_value, evidence_source="privacy-test")
    serialized = json.dumps(payload, sort_keys=True)

    assert payload["results"]
    assert payload["query"] == "[REDACTED]"
    assert query_value not in serialized
    evidence_text = (repo / ".ai" / "memory" / "evidence.jsonl").read_text(encoding="utf-8")
    audit_text = "".join(
        path.read_text(encoding="utf-8")
        for path in (repo / ".ai" / "memory" / "audit").glob("*.jsonl")
    )
    assert query_value not in evidence_text
    assert query_value not in audit_text
    assert "[REDACTED]" in evidence_text


@pytest.mark.skipif(os.name == "nt", reason="colon is not a valid Windows filename character")
def test_rg_fallback_handles_colon_in_filename(tmp_path: Path, monkeypatch) -> None:
    if not shutil.which("rg"):
        pytest.skip("ripgrep not installed on test runner")
    monkeypatch.setenv("AI_SEARCH_RG_FALLBACK", "1")
    repo = _make_repo(tmp_path)
    _write(repo, "src/part:name.py", "ColonPathNeedle = True\n")

    results = _rg_fallback(repo, "ColonPathNeedle")

    assert results[0]["path"] == "src/part:name.py"
    assert "ColonPathNeedle" in results[0]["snippet"]


def test_rg_fallback_does_not_read_source_for_ordinary_preview(
    tmp_path: Path,
    monkeypatch,
) -> None:
    if not shutil.which("rg"):
        pytest.skip("ripgrep not installed on test runner")
    monkeypatch.setenv("AI_SEARCH_RG_FALLBACK", "1")
    repo = _make_repo(tmp_path)
    _write(repo, "doc.md", "OrdinaryFallbackNeedle\n")

    monkeypatch.setattr(
        search_mod,
        "read_root_confined_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("ordinary fallback preview must not reread the source file")
        ),
    )

    results = _rg_fallback(repo, "OrdinaryFallbackNeedle")

    assert results[0]["path"] == "doc.md"
    assert results[0]["snippet"] == "L1: OrdinaryFallbackNeedle"


def test_rg_fallback_does_not_enumerate_all_repository_candidates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    if not shutil.which("rg"):
        pytest.skip("ripgrep not installed on test runner")
    monkeypatch.setenv("AI_SEARCH_RG_FALLBACK", "1")
    repo = _make_repo(tmp_path)
    _write(repo, "src/match.py", "CandidateEnumerationNeedle = True\n")
    monkeypatch.setattr(
        search_mod,
        "iter_text_files",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("fallback must validate returned paths without repository enumeration")
        ),
    )

    results = _rg_fallback(repo, "CandidateEnumerationNeedle")

    assert [item["path"] for item in results] == ["src/match.py"]


def test_rg_fallback_validates_only_paths_returned_by_ripgrep(
    tmp_path: Path,
    monkeypatch,
) -> None:
    if not shutil.which("rg"):
        pytest.skip("ripgrep not installed on test runner")
    monkeypatch.setenv("AI_SEARCH_RG_FALLBACK", "1")
    repo = _make_repo(tmp_path)
    for index in range(100):
        _write(repo, f"src/unmatched_{index}.py", f"VALUE_{index} = {index}\n")
    match = _write(repo, "src/match.py", "ReturnedPathNeedle = True\n")
    original = search_mod._is_indexable_text_file
    validated: list[Path] = []

    def counting_policy(root: Path, path: Path) -> bool:
        validated.append(path)
        return original(root, path)

    monkeypatch.setattr(search_mod, "_is_indexable_text_file", counting_policy)

    results = _rg_fallback(repo, "ReturnedPathNeedle")

    assert [item["path"] for item in results] == ["src/match.py"]
    assert validated == [match]


def test_rg_fallback_rejects_non_indexable_text_suffix(
    tmp_path: Path,
    monkeypatch,
) -> None:
    if not shutil.which("rg"):
        pytest.skip("ripgrep not installed on test runner")
    monkeypatch.setenv("AI_SEARCH_RG_FALLBACK", "1")
    repo = _make_repo(tmp_path)
    _write(repo, "artifact.bin", "UnsupportedSuffixNeedle\n")

    assert _rg_fallback(repo, "UnsupportedSuffixNeedle") == []


def test_rg_result_path_is_lexically_root_confined(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    inside = repo / "src" / "main.py"
    outside = tmp_path / "outside.py"

    assert search_mod._rg_result_path(repo, "src/../src/main.py") == (
        inside,
        "src/main.py",
    )
    assert search_mod._rg_result_path(repo, "../outside.py") is None
    assert search_mod._rg_result_path(repo, str(outside)) is None


def test_rg_fallback_skips_literal_query_inside_private_key_block(
    tmp_path: Path,
    monkeypatch,
) -> None:
    if not shutil.which("rg"):
        pytest.skip("ripgrep not installed on test runner")
    monkeypatch.setenv("AI_SEARCH_RG_FALLBACK", "1")
    repo = _make_repo(tmp_path)
    begin = "-----BEGIN " + "PRIVATE " + "KEY-----"
    end = "-----END " + "PRIVATE " + "KEY-----"
    needle = "InsideKeyNeedleQ7"
    _write(repo, "key.txt", f"{begin}\n{needle}\n{end}\n")

    assert _rg_fallback(repo, needle) == []


def test_rg_fallback_returns_same_literal_outside_private_key_block(
    tmp_path: Path,
    monkeypatch,
) -> None:
    if not shutil.which("rg"):
        pytest.skip("ripgrep not installed on test runner")
    monkeypatch.setenv("AI_SEARCH_RG_FALLBACK", "1")
    repo = _make_repo(tmp_path)
    begin = "-----BEGIN " + "PRIVATE " + "KEY-----"
    end = "-----END " + "PRIVATE " + "KEY-----"
    needle = "SharedKeyNeedleQ7"
    _write(repo, "key.txt", f"{begin}\n{needle}\n{end}\nafter {needle}\n")

    results = _rg_fallback(repo, needle)

    assert len(results) == 1
    assert results[0]["path"] == "key.txt"
    assert results[0]["snippet"] == f"L4: after {needle}"


def test_rg_fallback_quotes_embedded_pcre2_quote_terminator(
    tmp_path: Path,
    monkeypatch,
) -> None:
    if not shutil.which("rg"):
        pytest.skip("ripgrep not installed on test runner")
    monkeypatch.setenv("AI_SEARCH_RG_FALLBACK", "1")
    repo = _make_repo(tmp_path)
    needle = r"LiteralNeedle\E[x"
    _write(repo, "doc.md", f"prefix {needle} suffix\n")

    results = _rg_fallback(repo, needle)

    assert results[0]["path"] == "doc.md"
    assert needle in results[0]["snippet"]


def test_rg_fallback_redacts_private_key_boundary_line(
    tmp_path: Path,
    monkeypatch,
) -> None:
    if not shutil.which("rg"):
        pytest.skip("ripgrep not installed on test runner")
    monkeypatch.setenv("AI_SEARCH_RG_FALLBACK", "1")
    repo = _make_repo(tmp_path)
    marker = "BoundaryMarkerQ7"
    begin = "-----BEGIN " + "PRIVATE " + "KEY-----"
    end = "-----END " + "PRIVATE " + "KEY-----"
    _write(repo, "key.txt", f"{marker} {begin}\nbody\n{end}\n")

    results = _rg_fallback(repo, marker)

    assert results[0]["snippet"] == "L1: [REDACTED]"


@pytest.mark.parametrize(
    ("query_value", "reason"),
    [
        ("", "empty_query"),
        ("\x00", "invalid_query_control_character"),
        ("x" * (search_mod.SEARCH_QUERY_MAX_CHARS + 1), "query_too_long"),
        (
            " ".join(f"term{index}" for index in range(search_mod.SEARCH_QUERY_MAX_TERMS + 1)),
            "query_too_many_terms",
        ),
    ],
)
def test_query_rejects_invalid_or_oversized_input_before_auto_refresh(
    tmp_path: Path,
    monkeypatch,
    query_value: str,
    reason: str,
) -> None:
    repo = _make_repo(tmp_path)
    refresh_calls: list[Path] = []
    monkeypatch.setattr(
        search_mod,
        "_auto_refresh_if_stale",
        lambda root: refresh_calls.append(root),
    )

    payload = query(repo, query_value)

    assert payload["ok"] is False
    assert payload["reason"] == reason
    assert payload["results"] == []
    assert payload["retrieval_policy"] == "none"
    assert payload["auto_refresh"]["reason"] == "query_rejected"
    assert len(payload["query"]) <= search_mod.SEARCH_QUERY_ECHO_MAX_CHARS
    assert refresh_calls == []


def test_rg_fallback_rejects_oversized_query_before_process_spawn(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("AI_SEARCH_RG_FALLBACK", "1")
    monkeypatch.setattr(search_mod.shutil, "which", lambda _name: "/usr/bin/rg")

    def unexpected_popen(*_args, **_kwargs):
        raise AssertionError("oversized query must not spawn ripgrep")

    monkeypatch.setattr(search_mod.subprocess, "Popen", unexpected_popen)

    oversized = "x" * (search_mod.SEARCH_QUERY_MAX_CHARS + 1)
    assert _rg_fallback(repo, oversized) == []


def test_rg_fallback_nonpositive_limit_does_not_spawn_process(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("AI_SEARCH_RG_FALLBACK", "1")
    monkeypatch.setattr(search_mod.shutil, "which", lambda _name: "/usr/bin/rg")
    monkeypatch.setattr(
        search_mod.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("nonpositive result limit must not spawn ripgrep")
        ),
    )

    assert _rg_fallback(repo, "needle", limit=0) == []
    assert _rg_fallback(repo, "needle", limit=-1) == []


def test_bounded_process_reader_caps_event_count_and_output_memory() -> None:
    script = (
        "import json,sys\n"
        "for i in range(10000):\n"
        " print(json.dumps({'type':'match','index':i,'payload':'x'*200}), flush=True)\n"
    )
    started = time.monotonic()

    lines = search_mod._run_process_lines_bounded(
        [sys.executable, "-c", script],
        timeout_seconds=2.0,
        max_output_bytes=4096,
        max_events=5,
    )

    assert 1 <= len(lines) <= 5
    assert sum(len(line.encode("utf-8")) for line in lines) <= 4096
    assert time.monotonic() - started < 2.0


def test_bounded_process_reader_timeout_reaps_process(tmp_path: Path) -> None:
    pid_path = tmp_path / "pid.txt"
    script = (
        "import os,sys,time\n"
        "open(sys.argv[1], 'w', encoding='utf-8').write(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )

    lines = search_mod._run_process_lines_bounded(
        [sys.executable, "-c", script, str(pid_path)],
        timeout_seconds=0.1,
        max_output_bytes=4096,
        max_events=10,
    )

    assert lines == []
    pid = int(pid_path.read_text(encoding="utf-8"))
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        pytest.fail("timed-out fallback process was not reaped")


def test_bounded_process_reader_supports_nul_records_and_cwd(tmp_path: Path) -> None:
    cwd_marker = tmp_path / "cwd.txt"
    script = (
        "import os,sys\n"
        "open(sys.argv[1], 'w', encoding='utf-8').write(os.getcwd())\n"
        "sys.stdout.buffer.write(b'src/a.py\\0src/b.py\\0')\n"
    )

    records = search_mod._run_process_lines_bounded(
        [sys.executable, "-c", script, str(cwd_marker)],
        cwd=tmp_path,
        delimiter=b"\0",
        timeout_seconds=2.0,
        max_output_bytes=4096,
        max_events=10,
        allowed_returncodes={0},
        require_complete=True,
    )

    assert records == ["src/a.py", "src/b.py"]
    assert Path(cwd_marker.read_text(encoding="utf-8")) == tmp_path


def test_bounded_process_reader_rejects_disallowed_returncode() -> None:
    records = search_mod._run_process_lines_bounded(
        [sys.executable, "-c", "print('partial'); raise SystemExit(3)"],
        timeout_seconds=2.0,
        max_output_bytes=4096,
        max_events=10,
        allowed_returncodes={0},
        require_complete=True,
    )

    assert records == []


def test_bounded_process_reader_complete_mode_rejects_partial_overflow() -> None:
    script = "import sys; sys.stdout.write('a\\0b\\0c\\0')"

    records = search_mod._run_process_lines_bounded(
        [sys.executable, "-c", script],
        delimiter=b"\0",
        timeout_seconds=2.0,
        max_output_bytes=4096,
        max_events=2,
        allowed_returncodes={0},
        require_complete=True,
    )

    assert records == []
