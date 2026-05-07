from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core.search import (  # noqa: E402
    SCHEMA_VERSION,
    connect,
    init_schema,
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


def test_schema_version_is_three(tmp_path: Path) -> None:
    assert SCHEMA_VERSION == 3
    repo = _make_repo(tmp_path)
    _write(repo, "doc.md", "hello world\n")
    rebuild(repo)
    with connect(repo) as conn:
        version = int(conn.execute("pragma user_version").fetchone()[0])
    assert version == 3


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


def test_legacy_v2_cache_auto_migrates_to_v3(tmp_path: Path) -> None:
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
        assert version_after == 3

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
