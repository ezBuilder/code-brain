from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from ai_core import graph_context
from ai_core.search import connect, rebuild


def _build_repo(root: Path) -> None:
    src = root / "src"
    src.mkdir(parents=True)
    (src / "service.py").write_text(
        "def alpha():\n    return 1\n\ndef alpha_percent():\n    return 2\n",
        encoding="utf-8",
    )
    (root / ".ai" / "cache").mkdir(parents=True)
    rebuild(root)


def test_seed_paths_reject_escape_absolute_and_excess_count(tmp_path: Path) -> None:
    _build_repo(tmp_path)

    escaped = graph_context.pack_graph_context(tmp_path, seed_paths=["../outside.py"])
    absolute = graph_context.pack_graph_context(tmp_path, seed_paths=[str(tmp_path / "src" / "service.py")])
    excessive = graph_context.pack_graph_context(
        tmp_path,
        seed_paths=[f"src/{index}.py" for index in range(graph_context.MAX_SEED_PATHS + 1)],
    )

    assert escaped["ok"] is False and escaped["reason"] == "invalid_seed_path"
    assert absolute["ok"] is False and absolute["reason"] == "invalid_seed_path"
    assert excessive["ok"] is False and excessive["reason"] == "invalid_seed_path"


def test_symbol_query_like_metacharacters_are_literal(tmp_path: Path) -> None:
    _build_repo(tmp_path)

    wildcard = graph_context.pack_graph_context(tmp_path, symbol_query="%", limit=10)
    underscore = graph_context.pack_graph_context(tmp_path, symbol_query="alpha_", limit=10)

    assert wildcard["seed_symbols"] == []
    assert [item["qualname"] for item in underscore["seed_symbols"]] == ["alpha_percent"]


def test_symbol_query_is_bounded_before_database_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        graph_context,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid query must stop before SQLite")
        ),
    )

    payload = graph_context.pack_graph_context(
        tmp_path,
        symbol_query="x" * (graph_context.MAX_SYMBOL_QUERY_CHARS + 1),
    )

    assert payload["ok"] is False
    assert payload["reason"] == "invalid_symbol_query"


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_snippet_reader_rejects_symlink_without_external_read(tmp_path: Path) -> None:
    _build_repo(tmp_path)
    source = tmp_path / "src" / "service.py"
    external = tmp_path.parent / "outside-graph.py"
    external.write_text("def alpha():\n    return 'outside secret'\n", encoding="utf-8")
    source.unlink()
    source.symlink_to(external)

    payload = graph_context.pack_graph_context(tmp_path, symbol_query="alpha", limit=5)

    assert payload["ok"] is True
    assert payload["results"]
    assert all(item["snippet"] == "" for item in payload["results"] if item["path"] == "src/service.py")
    assert "outside secret" not in payload["additionalContext"]
    assert external.read_text(encoding="utf-8") == "def alpha():\n    return 'outside secret'\n"


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_snippet_reader_rejects_hardlink_without_external_read(tmp_path: Path) -> None:
    _build_repo(tmp_path)
    source = tmp_path / "src" / "service.py"
    external = tmp_path.parent / "outside-graph-hardlink.py"
    external.write_text("def alpha():\n    return 'outside secret'\n", encoding="utf-8")
    source.unlink()
    os.link(external, source)

    payload = graph_context.pack_graph_context(tmp_path, symbol_query="alpha", limit=5)

    assert payload["ok"] is True
    assert all(item["snippet"] == "" for item in payload["results"] if item["path"] == "src/service.py")
    assert "outside secret" not in payload["additionalContext"]


def test_snippet_source_is_read_once_per_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _build_repo(tmp_path)
    real_read = graph_context.read_root_confined_text
    calls: list[Path] = []

    def counting_read(path: Path, **kwargs):
        calls.append(path)
        return real_read(path, **kwargs)

    monkeypatch.setattr(graph_context, "read_root_confined_text", counting_read)

    payload = graph_context.pack_graph_context(tmp_path, seed_paths=["src/service.py"], limit=20)

    assert payload["ok"] is True
    assert calls.count(tmp_path / "src" / "service.py") == 1


def test_malicious_database_paths_are_dropped(tmp_path: Path) -> None:
    _build_repo(tmp_path)
    with connect(tmp_path) as conn:
        conn.execute(
            "insert into code_symbols(path, qualname, kind, lineno, end_lineno, parent, lang) values(?,?,?,?,?,?,?)",
            ("../outside.py", "malicious", "function", 1, 1, "", "python"),
        )
        conn.commit()

    payload = graph_context.pack_graph_context(tmp_path, symbol_query="malicious", limit=10)

    assert payload["ok"] is True
    assert payload["seed_symbols"] == []
    assert payload["results"] == []


def test_context_output_has_strict_byte_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _build_repo(tmp_path)
    monkeypatch.setattr(graph_context, "MAX_CONTEXT_BYTES", 80)

    payload = graph_context.pack_graph_context(tmp_path, seed_paths=["src/service.py"], limit=20)

    assert payload["ok"] is True
    assert len(payload["additionalContext"].encode("utf-8")) <= 80


def test_corrupt_index_fails_soft(tmp_path: Path) -> None:
    cache = tmp_path / ".ai" / "cache"
    cache.mkdir(parents=True)
    (cache / "code.sqlite").write_bytes(b"not sqlite")

    payload = graph_context.pack_graph_context(tmp_path, symbol_query="alpha")

    assert payload["ok"] is False
    assert payload["reason"] == "index_unavailable"
    assert payload["results"] == []
