"""Stage 1 dense hybrid end-to-end (mocked embeddings) — ingest→embed store→hybrid query fusion.

Drives the full active-dense path with deterministic vectors so the BM25∥dense→RRF pipeline
is exercised without ONNX deps. Verifies commit triggers embedding storage and query fuses both.
"""
from __future__ import annotations

from ai_core.autoresearch import ingest, query, dense, storage, fts


def test_dense_hybrid_end_to_end(tmp_path, monkeypatch):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    # force dense active with deterministic embeddings (alpha→[1,0], else→[0,1])
    monkeypatch.setattr(dense, "is_active_for", lambda r: True)
    monkeypatch.setattr(dense._emb, "embed_batch",
                        lambda texts, root: [[1.0, 0.0] if "alpha" in t else [0.0, 1.0] for t in texts])
    monkeypatch.setattr(dense, "embed_text",
                        lambda text, root: [1.0, 0.0] if "alpha" in text else [0.0, 1.0])

    st = ingest.stage_source(ar, content="shared corpus alpha beta source text")
    sid = st["source_id"]
    ingest.commit_pages(ar, source_id=sid, pages=[
        {"rel_path": "a.md", "content": "shared alpha", "sources": [sid]},
        {"rel_path": "b.md", "content": "shared beta", "sources": [sid]},
    ])
    # commit → dense embeddings stored for both pages (ingest→dense integration)
    conn = fts.connect(ar)
    assert dense.get_embedding(conn, "a.md") is not None
    assert dense.get_embedding(conn, "b.md") is not None
    conn.close()

    # hybrid query: BM25 ('shared' matches both pages) fused with dense ranking
    res = query.query(ar, "shared", k=5)
    pages = [c["page"] for c in res["candidates"]]
    assert "a.md" in pages and "b.md" in pages  # both retrieved and fused
    assert res["quarantined"] == []             # active pages, isolation intact


def test_dense_inactive_path_unchanged(tmp_path):
    # without forcing active, dense is off (no deps / small corpus) → pure BM25, no embeddings
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    st = ingest.stage_source(ar, content="plain bm25 corpus")
    ingest.commit_pages(ar, source_id=st["source_id"], pages=[
        {"rel_path": "p.md", "content": "plain bm25 page", "sources": [st["source_id"]]}])
    dense.init_embeddings(ar)
    conn = fts.connect(ar)
    assert dense.get_embedding(conn, "p.md") is None  # no embedding written (inactive)
    conn.close()
    res = query.query(ar, "bm25", k=5)
    assert any(c["page"] == "p.md" for c in res["candidates"])
