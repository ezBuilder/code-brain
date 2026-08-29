"""Regression coverage for bounded indexing of oversized source files."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ai_core import astgrep_integration, codegraph, search as search_module
from ai_core.doctor import check_index_coverage
from ai_core.obs import index_summary, search_report
from ai_core.search import (
    MAX_LARGE_SYMBOL_BYTES,
    MAX_LARGE_SYMBOL_CHUNKS,
    MAX_TEXT_BYTES,
    SOURCE_WINDOW_STEP_BYTES,
    _SourceWindowBuilder,
    db_path,
    index_diagnostics,
    index_hash_status,
    observability,
    query,
    rebuild,
)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".ai").mkdir(parents=True)
    (repo / ".ai" / "config.yaml").write_text("project_name: oversized\n", encoding="utf-8")
    return repo


def _write(repo: Path, rel: str, content: str | bytes) -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return path


def test_overlapping_windows_cover_all_bytes() -> None:
    source = "0123456789" * 30_000
    builder = _SourceWindowBuilder()
    windows = []
    for offset in range(0, len(source), 16_384):
        windows.extend(builder.feed(source[offset : offset + 16_384]))
    windows.extend(builder.finish())

    assert windows
    assert all(len(window.text.encode("utf-8")) <= MAX_TEXT_BYTES for window in windows)
    intervals = []
    for ordinal, window in enumerate(windows):
        start = ordinal * SOURCE_WINDOW_STEP_BYTES
        assert source[start : start + len(window.text)] == window.text
        intervals.append((start, start + len(window.text)))
    assert intervals[0][0] == 0
    assert intervals[-1][1] >= len(source)
    assert all(right >= next_left for (_left, right), (next_left, _right) in zip(intervals, intervals[1:]))


@pytest.mark.parametrize(
    ("rel", "prefix", "declaration", "needle"),
    [
        (
            "src/large.py",
            "# filler " + "x" * 80 + "\n",
            "def middle_python_canary(value):\n    return value\n",
            "middle_python_canary",
        ),
        (
            "src/large.rs",
            "// filler " + "x" * 80 + "\n",
            "fn middle_rust_canary(value: u32) -> u32 { value }\n",
            "middle_rust_canary",
        ),
        (
            "src/large.ts",
            "// filler " + "x" * 80 + "\n",
            "export function middle_typescript_canary(value: number) { return value; }\n",
            "middle_typescript_canary",
        ),
        (
            "src/large.js",
            "// filler " + "x" * 80 + "\n",
            "function middle_javascript_canary(value) { return value; }\n",
            "middle_javascript_canary",
        ),
        (
            "src/large.dart",
            "// filler " + "x" * 80 + "\n",
            "int middle_dart_canary(int value) { return value; }\n",
            "middle_dart_canary",
        ),
        (
            "src/large.kt",
            "// filler " + "x" * 80 + "\n",
            "fun middle_kotlin_canary(value: Int): Int { return value }\n",
            "middle_kotlin_canary",
        ),
    ],
)
def test_oversized_source_search_and_graph_recall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rel: str,
    prefix: str,
    declaration: str,
    needle: str,
) -> None:
    repo = _make_repo(tmp_path)
    source = _write(repo, rel, prefix * 1_500 + declaration)
    monkeypatch.setenv("AI_SEARCH_AUTO_REFRESH", "0")
    monkeypatch.setenv("AI_SEARCH_CODEGRAPH", "1")

    result = rebuild(repo)

    assert result["ok"] is True
    assert result["skipped"] == []
    assert source.stat().st_size > MAX_TEXT_BYTES
    search_result = query(repo, needle, limit=10)
    result_paths = [item["path"] for item in search_result["results"]]
    assert result_paths.count(rel) == 1
    assert all("::window:" not in path and path != f"{rel}:{needle}" for path in result_paths)

    symbols = codegraph.find_symbol(repo, needle, limit=10)
    assert symbols["ok"] is True
    assert symbols["count"] == 1
    assert symbols["symbols"][0]["path"] == rel

    with sqlite3.connect(db_path(repo)) as conn:
        rows = conn.execute(
            "select c.path, m.kind, m.start_line, m.end_line "
            "from chunks c join chunk_meta m on m.chunk_id = c.id "
            "where c.path = ? or c.path like ? order by c.id",
            (rel, rel + "%"),
        ).fetchall()
    assert sum(kind == "file" for _path, kind, _start, _end in rows) == 1
    assert sum(kind == "file_window" for _path, kind, _start, _end in rows) >= 1
    assert any(kind in {"function", "class", "method"} for _path, kind, _start, _end in rows)
    coverage = index_diagnostics(repo)
    assert coverage["silent_skip_count"] == 0
    assert coverage["unindexed"] == []


def test_large_private_key_redaction_preserves_original_graph_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    hidden_block_text = "PRIVATE_" + "PAYLOAD_MUST_NOT_BE_INDEXED"
    lines = ["# filler " + "x" * 80] * 1_200
    lines.extend(
        [
            "-----BEGIN " + "PRIVATE " + "KEY-----",
            hidden_block_text,
            "private body line 2",
            "-----END " + "PRIVATE " + "KEY-----",
            "api_key = '" + "z" * 32 + "'",
            "# after secret block",
            "def private_block_middle_canary():",
            "    return 1",
        ]
    )
    source = _write(repo, "src/private_large.py", "\n".join(lines) + "\n")
    original_line = next(
        index for index, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1)
        if line.startswith("def private_block_middle_canary")
    )
    assert source.stat().st_size > MAX_TEXT_BYTES
    monkeypatch.setenv("AI_SEARCH_AUTO_REFRESH", "0")
    monkeypatch.setenv("AI_SEARCH_CODEGRAPH", "1")

    assert rebuild(repo)["ok"] is True
    symbols = codegraph.find_symbol(repo, "private_block_middle_canary", limit=10)
    assert symbols["symbols"][0]["lineno"] == original_line
    visible = query(repo, "private_block_middle_canary", limit=5)
    serialized = json.dumps(visible, sort_keys=True)
    assert hidden_block_text not in serialized
    assert "z" * 32 not in serialized


def test_oversized_long_single_line_and_nested_braces_are_searchable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    long_line = (
        "/* "
        + "x" * 120_000
        + " */ function long_line_nested_canary(value) { "
        + "if (value) { if (value > 1) { return value; } } return 0; }\n"
    )
    source = _write(repo, "src/long_line.js", long_line)
    assert source.stat().st_size > MAX_TEXT_BYTES
    monkeypatch.setenv("AI_SEARCH_AUTO_REFRESH", "0")
    monkeypatch.setenv("AI_SEARCH_CODEGRAPH", "1")

    assert rebuild(repo)["ok"] is True
    result = query(repo, "long_line_nested_canary", limit=10)
    assert [item["path"] for item in result["results"]].count("src/long_line.js") == 1
    symbols = codegraph.find_symbol(repo, "long_line_nested_canary", limit=10)
    assert symbols["symbols"][0]["path"] == "src/long_line.js"
    assert symbols["symbols"][0]["lineno"] == 1


def test_large_symbol_chunks_have_file_budget_and_windows_retain_recall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    functions: list[str] = []
    for index in range(300):
        functions.append(
            f"def budget_{index}():\n"
            "    data = \"" + "x" * 9_000 + "\"\n"
            "    return data\n"
        )
    source = _write(repo, "src/symbol_budget.py", "".join(functions))
    monkeypatch.setenv("AI_SEARCH_AUTO_REFRESH", "0")
    monkeypatch.setenv("AI_SEARCH_CODEGRAPH", "1")

    result = rebuild(repo)

    assert result["ok"] is True
    assert result["symbol_budget"]
    budget = result["symbol_budget"][0]
    assert int(budget["budget_skipped"]) > 0
    with sqlite3.connect(db_path(repo)) as conn:
        count, total_bytes = conn.execute(
            "select count(*), coalesce(sum(m.bytes), 0) "
            "from chunks c join chunk_meta m on m.chunk_id = c.id "
            "where c.path like 'src/symbol_budget.py:%' and m.kind = 'function'"
        ).fetchone()
    assert count <= MAX_LARGE_SYMBOL_CHUNKS
    assert total_bytes <= MAX_LARGE_SYMBOL_BYTES
    recall = query(repo, "budget_299", limit=10)
    assert [item["path"] for item in recall["results"]].count("src/symbol_budget.py") == 1


def test_diagnostics_expose_generated_binary_lock_and_encoding_classes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    _write(repo, "dist/app.bundle.js", "generated_body_must_not_be_indexed\n")
    _write(repo, "src/state.lock", "lock body\n")
    _write(repo, "src/artifact.bin", b"\x00\xff\x00")
    _write(repo, "src/nul.py", b"source\x00content")
    _write(repo, "src/invalid.py", b"valid " + (b"x" * 100_001) + b"\xffsource")
    _write(repo, "src/unknown.xyz", "unsupported\n")
    monkeypatch.setenv("AI_SEARCH_AUTO_REFRESH", "0")

    result = rebuild(repo)
    report = index_diagnostics(repo)
    classes = {
        item["path"]: (item["class"], item["reason"])
        for item in report["unindexed"]
    }

    assert result["ok"] is True
    assert classes["src/state.lock"][0] == "lock"
    assert classes["src/artifact.bin"][0] == "binary_data"
    assert classes["src/nul.py"] == ("binary_data", "binary_or_data_content")
    assert classes["src/invalid.py"] == ("encoding", "invalid_utf8")
    assert classes["src/unknown.xyz"][0] == "unsupported"
    assert report["silent_skip_count"] == 0
    assert report["classification_stubs"] == [
        {
            "path": "dist/app.bundle.js",
            "class": "generated",
            "reason": "generated_artifact_body_omitted",
            "indexed_as": "path_stub",
        }
    ]
    assert query(repo, "generated_body_must_not_be_indexed")["results"] == []
    assert query(repo, "dist app")["results"][0]["path"] == "dist/app.bundle.js"
    doctor_check = check_index_coverage(repo)
    assert doctor_check.ok is True
    assert "invalid.py:encoding:invalid_utf8" in doctor_check.detail


def test_binary_source_candidate_hash_status_converges_and_reactivates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    rel = "src/generated_fixture.ts"
    source = _write(repo, rel, b"fixture\x00payload")
    monkeypatch.setenv("AI_SEARCH_AUTO_REFRESH", "0")
    monkeypatch.setenv("AI_SEARCH_RG_FALLBACK", "0")

    first = rebuild(repo)
    assert first["ok"] is True
    assert any(item["path"] == rel for item in first["skipped"])
    assert index_hash_status(repo)["ok"] is True
    assert {item["path"]: item["class"] for item in index_diagnostics(repo)["unindexed"]}[rel] == "binary_data"

    source.write_text("export const recovered_source_canary = true;\n", encoding="utf-8")
    recovered_status = index_hash_status(repo)
    assert recovered_status["changed_paths"] == [rel]
    assert rebuild(repo, incremental=True, paths={rel})["added"] == 1
    assert query(repo, "recovered_source_canary", limit=5)["results"][0]["path"] == rel
    assert index_hash_status(repo)["ok"] is True

    source.write_bytes(b"fixture\x00payload")
    binary_status = index_hash_status(repo)
    assert binary_status["changed_paths"] == [rel]
    retired = rebuild(repo, incremental=True, paths={rel})
    assert retired["skipped_count"] == 1
    assert query(repo, "recovered_source_canary", limit=5)["results"] == []
    assert index_hash_status(repo)["ok"] is True

    source.write_text("export const reactivated_source_canary = true;\n", encoding="utf-8")
    reactivated_status = index_hash_status(repo)
    assert reactivated_status["changed_paths"] == [rel]
    assert rebuild(repo, incremental=True, paths={rel})["added"] == 1
    assert query(repo, "reactivated_source_canary", limit=5)["results"][0]["path"] == rel
    assert index_hash_status(repo)["ok"] is True


def test_self_index_canary_asserts_the_returned_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    _write(repo, "src/hooks.py", "def self_index_path_canary():\n    return True\n")
    monkeypatch.setenv("AI_SEARCH_AUTO_REFRESH", "0")

    assert rebuild(repo)["ok"] is True
    result = query(repo, "self_index_path_canary", limit=5)

    assert result["results"]
    assert result["results"][0]["path"] == "src/hooks.py"


def test_query_reads_each_large_source_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    _write(
        repo,
        "src/cache_large.py",
        ("# repeated_cache_canary " + "x" * 80 + "\n") * 1_500
        + "def repeated_cache_canary():\n    return True\n",
    )
    monkeypatch.setenv("AI_SEARCH_AUTO_REFRESH", "0")
    assert rebuild(repo)["ok"] is True
    original = search_module.snippet_from_file
    calls: list[str] = []

    def counted(*args, **kwargs):
        calls.append(str(args[1]))
        return original(*args, **kwargs)

    monkeypatch.setattr(search_module, "snippet_from_file", counted)
    result = query(repo, "repeated_cache_canary", limit=10)

    assert result["results"][0]["path"] == "src/cache_large.py"
    assert calls.count("src/cache_large.py") == 1


def test_large_file_windows_do_not_crowd_out_other_source_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    _write(
        repo,
        "src/a_large.py",
        ("# shared_diversity_canary " + "x" * 80 + "\n") * 4_000,
    )
    _write(repo, "src/z_small.py", "# shared_diversity_canary\n")
    monkeypatch.setenv("AI_SEARCH_AUTO_REFRESH", "0")
    monkeypatch.setenv("AI_SEARCH_RG_FALLBACK", "0")
    assert rebuild(repo)["ok"] is True

    result = query(repo, "shared_diversity_canary", limit=2)

    assert [item["path"] for item in result["results"]] == [
        "src/a_large.py",
        "src/z_small.py",
    ]


def test_observability_and_mcp_search_bytes_count_source_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    source = _write(
        repo,
        "src/mcp_large.py",
        ("# filler " + "x" * 80 + "\n") * 1_500
        + "def mcp_large_canary():\n    return 'mcp'\n",
    )
    monkeypatch.setenv("AI_SEARCH_AUTO_REFRESH", "0")
    monkeypatch.setenv("AI_SEARCH_CODEGRAPH", "1")
    assert rebuild(repo)["ok"] is True

    expected_source_bytes = sum(
        path.stat().st_size
        for path in (repo / ".ai" / "config.yaml", source)
    )
    observed = observability(repo)
    summary = index_summary(repo)
    mcp_report = search_report(repo, query_text="mcp_large_canary", limit=10)

    assert observed["indexed_bytes"] == expected_source_bytes
    assert observed["indexed_source_bytes"] == expected_source_bytes
    assert summary["indexed_bytes"] == expected_source_bytes
    assert mcp_report["query"]["result_paths"] == ["src/mcp_large.py"]
    assert mcp_report["query"]["matched_indexed_bytes"] == source.stat().st_size


def test_large_ast_search_reads_streamed_redacted_source_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    source = _write(
        repo,
        "src/ast_large.js",
        ("// filler " + "x" * 80 + "\n") * 1_500
        + "function ast_stream_canary(value) { return value; }\n",
    )
    captured: dict[str, object] = {}

    def fake_scan(path: Path, _rule_yaml: str, *, timeout_seconds: float) -> list[dict[str, object]]:
        captured["path"] = path
        captured["body"] = path.read_text(encoding="utf-8")
        return [
            {
                "file": str(path),
                "range": {"start": {"line": 0, "column": 0}},
                "text": "function ast_stream_canary",
            }
        ]

    monkeypatch.setattr(astgrep_integration, "astgrep_available", lambda: True)
    monkeypatch.setattr(astgrep_integration, "scan_path", fake_scan)

    result = astgrep_integration.ast_grep_search(
        repo,
        pattern="function $NAME($$$ARGS)",
        lang="javascript",
        path="src/ast_large.js",
    )

    assert result["ok"] is True
    assert result["matches"][0]["file"] == "src/ast_large.js"
    assert "ast_stream_canary" in str(captured["body"])
    assert Path(str(captured["path"])) != source
