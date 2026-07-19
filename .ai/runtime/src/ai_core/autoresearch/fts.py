"""FTS5 BM25 indexing for the autoresearch corpus (stdlib sqlite3, no-deps).

Reuses the contentless-fts5 + porter tokenizer pattern from search.py:create_schema.
Stage 0 = BM25 only. _compute_rrf_k / dense / rerank already live in search.py and
are wired in Stage 1 — do NOT reimplement here (PRD §12.2.8). FTS is a DERIVED index
(git is SSOT for wiki/); it can always be rebuilt from markdown via rebuild_index().
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from . import storage

_SCHEMA = """
create table if not exists pages (
  page_id text primary key,
  rel_path text not null,
  sha256 text not null,
  updated_at text default current_timestamp
);
create virtual table if not exists pages_fts using fts5(
  page_id unindexed, rel_path, content,
  tokenize="porter unicode61 remove_diacritics 2"
);
"""


def connect(root: Path) -> sqlite3.Connection:
    storage.ensure_tree(root)
    conn = sqlite3.connect(storage.fts_db_path(root))
    conn.execute("pragma journal_mode=WAL;")
    conn.execute("pragma busy_timeout=5000;")
    return conn


def init_fts(root: Path) -> None:
    conn = connect(root)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def upsert_page(conn: sqlite3.Connection, page_id: str, rel_path: str, sha256: str, content: str) -> None:
    conn.execute(
        "insert into pages(page_id, rel_path, sha256) values(?,?,?) "
        "on conflict(page_id) do update set rel_path=excluded.rel_path, "
        "sha256=excluded.sha256, updated_at=current_timestamp",
        (page_id, rel_path, sha256),
    )
    conn.execute("delete from pages_fts where page_id=?", (page_id,))
    conn.execute(
        "insert into pages_fts(page_id, rel_path, content) values(?,?,?)",
        (page_id, rel_path, content),
    )


def search(root: Path, query: str, k: int = 10) -> list[dict]:
    """BM25 query. bm25() returns lower=better, so ascending order is most-relevant first."""
    if not storage.fts_db_path(root).is_file():
        return []
    query_text = str(query or "").strip()
    if not query_text:
        return []
    # Natural-language questions routinely contain FTS5 operators or punctuation
    # (`/`, `:`, `-`, `?`). Reuse the main code-search normalizer so those inputs
    # become quoted OR terms instead of parser errors.
    from ..search import escape_fts_query

    conn = connect(root)
    try:
        try:
            cur = conn.execute(
                "select page_id, rel_path, "
                "snippet(pages_fts, 2, '[', ']', '…', 12) as snip, "
                "bm25(pages_fts) as score "
                "from pages_fts where pages_fts match ? order by score limit ?",
                (escape_fts_query(query_text), k),
            )
            rows = cur.fetchall()
        except sqlite3.OperationalError:
            return [{"error": "fts_query_failed"}]  # generic — no path/internal disclosure
        return [
            {"page": r[1], "page_id": r[0], "snippet": r[2], "score": r[3]}
            for r in rows
        ]
    finally:
        conn.close()


def rebuild_index(root: Path) -> int:
    """Rebuild the FTS index from wiki/ markdown. Returns page count (DERIVED — safe to drop)."""
    init_fts(root)
    conn = connect(root)
    count = 0
    try:
        conn.execute("delete from pages")
        conn.execute("delete from pages_fts")
        wiki = storage.wiki_root(root)
        if wiki.is_dir():
            for md in sorted(wiki.rglob("*.md")):
                if md.name == storage.LOG_NAME:  # exclude the append-only chronicle
                    continue
                rel = str(md.relative_to(wiki))
                content = md.read_text(encoding="utf-8", errors="replace")
                sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
                upsert_page(conn, rel, rel, sha, content)
                count += 1
        conn.commit()
    finally:
        conn.close()
    return count
