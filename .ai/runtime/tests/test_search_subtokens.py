"""Identifier-subtoken dual emission (schema v9) and legacy-schema query self-heal."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core.search import (  # noqa: E402
    SUBTOKEN_MAX_TERMS,
    _fts_document,
    connect,
    identifier_subtokens,
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


def test_identifier_subtokens_split_camel_acronym_and_digits() -> None:
    parts = identifier_subtokens("additionalContext = HTTPServer(parseJSON2CSV)")
    assert "additional" in parts
    assert "context" in parts
    assert "http" in parts
    assert "server" in parts
    assert "parse" in parts
    assert "json" in parts
    assert "csv" in parts


def test_identifier_subtokens_skip_boundaryless_words() -> None:
    # all-lower, ALL-UPPER, and Capitalized single words add no new subtokens
    assert identifier_subtokens("plain lowercase WORDS Capitalized") == []


def test_identifier_subtokens_dedupe_and_cap() -> None:
    text = "aliceBob " * 50
    assert identifier_subtokens(text) == ["alice", "bob"]
    many = " ".join(f"prefixWord{i}Suffix{i}" for i in range(SUBTOKEN_MAX_TERMS * 2))
    assert len(identifier_subtokens(many)) <= SUBTOKEN_MAX_TERMS


def test_fts_document_appends_subtokens_and_respects_kill_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AI_SEARCH_SUBTOKENS", raising=False)
    doc = _fts_document("def handleUserPayload():\n    pass\n")
    assert doc.startswith("def handleUserPayload():")
    assert "handle user payload" in doc.rsplit("\n", 1)[-1].replace("  ", " ")
    monkeypatch.setenv("AI_SEARCH_SUBTOKENS", "0")
    raw = "def handleUserPayload():\n    pass\n"
    assert _fts_document(raw) == raw


def test_split_word_query_matches_camel_case_identifier(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _write(
        repo,
        "src/service.js",
        "function fetchFlightScheduleBoard(remoteGateway) {\n"
        "  return remoteGateway.loadDepartures();\n"
        "}\n",
    )
    _write(repo, "docs/noise.md", "unrelated ferry timetable notes\n")
    rebuild(repo)
    payload = query(repo, "flight schedule board", limit=3)
    assert payload["ok"] is True
    assert any(item["path"].startswith("src/service.js") for item in payload["results"])


def test_kill_switch_restores_previous_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AI_SEARCH_SUBTOKENS", "0")
    repo = _make_repo(tmp_path)
    _write(
        repo,
        "src/service.js",
        "function fetchFlightScheduleBoard(remoteGateway) {\n"
        "  return remoteGateway.loadDepartures();\n"
        "}\n",
    )
    rebuild(repo)
    payload = query(repo, "flight schedule board", limit=3)
    assert not any(
        item["path"].startswith("src/service.js") for item in payload["results"]
    )


def test_query_self_heals_outdated_schema_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path)
    _write(repo, "src/board.js", "function fetchFlightScheduleBoard() {}\n")
    rebuild(repo)
    with connect(repo) as conn:
        conn.execute("pragma user_version=5")
        conn.commit()
    # Force the load path (not auto-refresh) to encounter the outdated index.
    monkeypatch.setenv("AI_SEARCH_AUTO_REFRESH", "0")
    payload = query(repo, "flight schedule board", limit=3)
    assert payload["ok"] is True
    assert payload["auto_refresh"]["reason"] == "outdated_schema"
    assert payload["auto_refresh"]["rebuilt"] is True
    assert any(item["path"].startswith("src/board.js") for item in payload["results"])
    with connect(repo) as conn:
        version = int(conn.execute("pragma user_version").fetchone()[0])
    assert version == 10


def test_structural_legacy_schema_still_requires_explicit_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path)
    db = repo / ".ai" / "cache" / "code.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            create table chunks (
              id integer primary key,
              path text not null,
              sha256 text not null,
              content text not null,
              updated_at text default current_timestamp
            );
            create virtual table chunks_fts using fts5(path, content, content='chunks', content_rowid='id');
            """
        )
    monkeypatch.setenv("AI_SEARCH_AUTO_REFRESH", "0")
    with pytest.raises(RuntimeError, match="legacy search index schema"):
        query(repo, "anything", limit=3)
    with sqlite3.connect(db) as conn:
        columns = [row[1] for row in conn.execute("pragma table_info(chunks)").fetchall()]
    assert "content" in columns  # not dropped by the read path


def test_non_schema_runtime_error_still_propagates(tmp_path: Path) -> None:
    from ai_core import search as search_mod

    exc = RuntimeError("something else entirely")
    assert search_mod._is_legacy_schema_error(exc) is False
    assert search_mod._is_outdated_schema_error(exc) is False
    assert search_mod._is_legacy_schema_error(
        RuntimeError("legacy search index schema; run ai index rebuild")
    ) is True
    assert search_mod._is_outdated_schema_error(
        RuntimeError("outdated search index schema; run ai index rebuild")
    ) is True
    assert search_mod._is_legacy_schema_error(sqlite3.OperationalError("x")) is False


def test_observability_degrades_on_outdated_schema_instead_of_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_core.search import observability

    repo = _make_repo(tmp_path)
    _write(repo, "src/board.js", "function fetchFlightScheduleBoard() {}\n")
    rebuild(repo)
    with connect(repo) as conn:
        conn.execute("pragma user_version=5")
        conn.commit()
    payload = observability(repo)
    assert payload["ok"] is False
    assert payload["reason"] == "outdated_schema"
