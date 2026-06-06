"""Dense embedding layer tests (Stage 1, opt-in) — no-op without deps, storage roundtrip."""
from __future__ import annotations

from ai_core.autoresearch import dense, fts, storage


def test_pack_unpack_roundtrip():
    v = [0.1, -0.2, 0.3, 0.456]
    out = dense._unpack(dense._pack(v))
    assert len(out) == len(v) and all(abs(a - b) < 1e-6 for a, b in zip(out, v))


def test_store_and_get_embedding(tmp_path):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    dense.init_embeddings(ar)
    conn = fts.connect(ar)
    dense.store_embedding(conn, "concepts/a.md", [0.1, 0.2, 0.3])
    conn.commit()
    got = dense.get_embedding(conn, "concepts/a.md")
    conn.close()
    assert got is not None and len(got) == 3 and abs(got[0] - 0.1) < 1e-6


def test_store_embedding_upsert(tmp_path):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    dense.init_embeddings(ar)
    conn = fts.connect(ar)
    dense.store_embedding(conn, "p.md", [1.0, 2.0])
    dense.store_embedding(conn, "p.md", [9.0, 9.0])  # overwrite
    conn.commit()
    got = dense.get_embedding(conn, "p.md")
    conn.close()
    assert got == [9.0, 9.0]


def test_get_embedding_missing(tmp_path):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    dense.init_embeddings(ar)
    conn = fts.connect(ar)
    assert dense.get_embedding(conn, "nope.md") is None
    conn.close()


def test_is_active_for_small_corpus_off(tmp_path):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    # tiny corpus → below 50K-token threshold → off regardless of deps (PRD §4.6)
    assert dense.is_active_for(ar) is False


def test_embed_text_noop_without_deps(tmp_path):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    # no ONNX deps in this env → embed returns None (BM25-only path unaffected)
    assert dense.embed_text("hello world", ar) is None


def test_embed_and_store_noop_inactive(tmp_path):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    assert dense.embed_and_store_pages(ar, [("a.md", "text")]) == 0


def test_embed_and_store_active_mock(tmp_path, monkeypatch):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    monkeypatch.setattr(dense, "is_active_for", lambda r: True)
    monkeypatch.setattr(dense._emb, "embed_batch", lambda texts, root: [[0.1, 0.2] for _ in texts])
    n = dense.embed_and_store_pages(ar, [("a.md", "alpha"), ("b.md", "beta")])
    assert n == 2
    conn = fts.connect(ar)
    got = dense.get_embedding(conn, "a.md")
    conn.close()
    assert got is not None and abs(got[0] - 0.1) < 1e-6


def test_rebuild_embeddings_noop_when_inactive(tmp_path):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    (storage.wiki_root(ar) / "concepts" / "a.md").parent.mkdir(parents=True, exist_ok=True)
    (storage.wiki_root(ar) / "concepts" / "a.md").write_text("content", encoding="utf-8")
    assert dense.rebuild_embeddings(ar) == 0  # inactive → no-op
