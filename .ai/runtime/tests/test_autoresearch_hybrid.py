"""Hybrid search tests (Stage 1) — BM25-only default + RRF fusion when dense mocked active."""
from __future__ import annotations

from ai_core.autoresearch import hybrid, ingest, storage, fts, dense


def _seed(ar):
    st = ingest.stage_source(ar, content="reciprocal rank fusion bm25 dense retrieval source")
    sid = st["source_id"]
    ingest.commit_pages(ar, source_id=sid, pages=[
        {"rel_path": "concepts/a.md", "content": "reciprocal rank fusion bm25", "sources": [sid]},
        {"rel_path": "concepts/b.md", "content": "dense retrieval embeddings", "sources": [sid]},
    ])


def test_hybrid_bm25_only_when_dense_inactive(tmp_path):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    _seed(ar)
    res = hybrid.search(ar, "fusion", k=5)
    assert res and res[0]["page"] == "concepts/a.md"
    # identical to plain BM25 because dense is inactive in this env
    assert [r["page"] for r in res] == [r["page"] for r in fts.search(ar, "fusion", k=5)]


def test_hybrid_empty(tmp_path):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    fts.init_fts(ar)
    assert hybrid.search(ar, "anything", k=5) == []


def test_hybrid_fuses_when_dense_active(tmp_path, monkeypatch):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    _seed(ar)
    # force dense active with deterministic query vector
    monkeypatch.setattr(hybrid.dense_mod, "is_active_for", lambda r: True)
    monkeypatch.setattr(hybrid.dense_mod, "embed_text", lambda q, r: [1.0, 0.0])
    dense.init_embeddings(ar)
    conn = fts.connect(ar)
    dense.store_embedding(conn, "concepts/a.md", [1.0, 0.0])  # cosine 1 with query
    dense.store_embedding(conn, "concepts/b.md", [0.0, 1.0])  # cosine 0
    conn.commit()
    conn.close()
    # query matches both via BM25 ("retrieval"/"fusion" share source); fusion returns both
    res = hybrid.search(ar, "retrieval", k=5)
    pages = [r["page"] for r in res]
    assert "concepts/b.md" in pages  # BM25 hit on 'retrieval'
    # output shape preserved (page/snippet present)
    assert all("page" in r for r in res)


def test_hybrid_dim_mismatch_degrades_safely(tmp_path, monkeypatch):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    _seed(ar)
    monkeypatch.setattr(hybrid.dense_mod, "is_active_for", lambda r: True)
    monkeypatch.setattr(hybrid.dense_mod, "embed_text", lambda q, r: [1.0, 0.0])  # dim 2
    dense.init_embeddings(ar)
    conn = fts.connect(ar)
    dense.store_embedding(conn, "concepts/a.md", [1.0, 2.0, 3.0])  # dim 3 — mismatch
    conn.commit()
    conn.close()
    res = hybrid.search(ar, "retrieval", k=5)  # must not crash; degrade to BM25
    assert res and all("page" in r for r in res)
    # equivalent to BM25 ordering since dense signal is dropped on mismatch
    assert {r["page"] for r in res} == {r["page"] for r in fts.search(ar, "retrieval", k=5)}
