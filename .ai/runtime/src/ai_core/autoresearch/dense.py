"""Dense embedding layer for AutoResearch hybrid search (Stage 1, opt-in, no-deps default).

Thin wrapper over the existing ONNX MiniLM embedder (embedding.py, AI_SEARCH_DENSE). When
deps/model are absent — or the corpus is below the Stage 1 threshold — it is a strict no-op
(returns None / 0), so the BM25-only Stage 0 path is unaffected (PRD §4.6). Vectors live in
autoresearch index/fts.db (embeddings_vec0), keyed by page_id: a DERIVED artifact rebuilt
from wiki/ alongside the FTS index. stdlib + reuse only.
"""
from __future__ import annotations

import struct
from pathlib import Path

from . import storage, fts as fts_mod
from .. import embedding as _emb

_EMB_SCHEMA = """
create table if not exists embeddings_vec0 (
  page_id text primary key,
  vector blob,
  model_name text,
  vector_dim integer,
  created_at text default current_timestamp
);
"""

CORPUS_THRESHOLD_TOKENS = 50_000  # PRD §4.6 — dense stays off below this


def _project_root(ar_root: Path) -> Path:
    return ar_root.parent.parent  # <proj>/.ai/autoresearch → <proj>


def init_embeddings(ar_root: Path) -> None:
    conn = fts_mod.connect(ar_root)
    try:
        conn.executescript(_EMB_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _corpus_tokens(ar_root: Path) -> int:
    """Approx token count of the wiki corpus (chars/4). Cheap gate, not exact."""
    wiki = storage.wiki_root(ar_root)
    if not wiki.is_dir():
        return 0
    total = 0
    for md in wiki.rglob("*.md"):
        if md.name == storage.LOG_NAME:
            continue
        try:
            total += len(md.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    return total // 4


def is_active_for(ar_root: Path) -> bool:
    """Dense fires only when corpus exceeds threshold AND the embedder is available
    (deps + model + AI_SEARCH_DENSE policy). Below threshold → BM25-only (§4.6)."""
    if _corpus_tokens(ar_root) < CORPUS_THRESHOLD_TOKENS:
        return False
    return _emb.is_active_for(_project_root(ar_root))


def embed_text(text: str, ar_root: Path) -> list[float] | None:
    """Embed one text via the shared ONNX embedder; None when dense unavailable (no-op)."""
    return _emb.embed(text, _project_root(ar_root))


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def store_embedding(conn, page_id: str, vector: list[float]) -> None:
    conn.execute(
        "insert into embeddings_vec0(page_id, vector, model_name, vector_dim) values(?,?,?,?) "
        "on conflict(page_id) do update set vector=excluded.vector, "
        "model_name=excluded.model_name, vector_dim=excluded.vector_dim",
        (page_id, _pack(vector), _emb.MODEL_NAME, len(vector)),
    )


def get_embedding(conn, page_id: str) -> list[float] | None:
    cur = conn.execute("select vector from embeddings_vec0 where page_id=?", (page_id,))
    row = cur.fetchone()
    return _unpack(row[0]) if row and row[0] is not None else None


def rebuild_embeddings(ar_root: Path) -> int:
    """Re-embed all wiki pages and store vectors (batched). No-op (0) when dense inactive.
    DERIVED — safe to drop and regenerate from wiki/."""
    if not is_active_for(ar_root):
        return 0
    init_embeddings(ar_root)
    wiki = storage.wiki_root(ar_root)
    pages = []
    for md in sorted(wiki.rglob("*.md")):
        if md.name == storage.LOG_NAME:
            continue
        pages.append((str(md.relative_to(wiki)), md.read_text(encoding="utf-8", errors="replace")))
    if not pages:
        return 0
    vecs = _emb.embed_batch([t for _, t in pages], _project_root(ar_root))
    if vecs is None:
        return 0
    conn = fts_mod.connect(ar_root)
    count = 0
    try:
        for (rel, _), vec in zip(pages, vecs):
            store_embedding(conn, rel, vec)
            count += 1
        conn.commit()
    finally:
        conn.close()
    return count
