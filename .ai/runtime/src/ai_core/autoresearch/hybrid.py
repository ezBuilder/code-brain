"""Hybrid search for AutoResearch (Stage 1, opt-in). BM25 ∥ dense → RRF → optional rerank.

When dense is active (corpus ≥ threshold + deps + model), fuse FTS5 BM25 ranking with dense
cosine ranking over the BM25 candidate pool via RRF (rrf.py), then optionally rerank the
shortlist (reranker_ar). When dense is inactive, return plain BM25 (fts.search) unchanged —
the always-on Stage 0 path. Reuses search.py RRF + embedding + reranker (PRD §12.2.8 — no
reimplementation). Output shape is identical to fts.search so query.py can consume either.
"""
from __future__ import annotations

from pathlib import Path

from . import fts as fts_mod, dense as dense_mod, rrf as rrf_mod, reranker_ar

_FUSION_POOL = 30  # widen BM25 candidates before fusion (dense may re-rank within pool)


def _cosine(a: list[float], b: list[float]) -> float:
    # embedding vectors are L2-normalized → dot product equals cosine similarity
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def _pid(row: dict) -> str:
    return str(row.get("page_id") or row.get("page") or "")


def search(ar_root: Path, query: str, k: int = 10) -> list[dict]:
    """Hybrid BM25+dense+rerank when active; plain BM25 otherwise. Same shape as fts.search."""
    bm25 = fts_mod.search(ar_root, query, k=max(k, _FUSION_POOL))
    if bm25 and isinstance(bm25[0], dict) and "error" in bm25[0]:
        return bm25
    if not bm25 or not dense_mod.is_active_for(ar_root):
        return bm25[:k]

    qvec = dense_mod.embed_text(query, ar_root)
    if qvec is None:
        return bm25[:k]

    qdim = len(qvec)
    dense_mod.init_embeddings(ar_root)  # ensure embeddings table exists (idempotent)
    conn = fts_mod.connect(ar_root)
    try:
        dense_scored = []
        for r in bm25:
            pid = _pid(r)
            if not pid:
                continue  # unidentifiable row — can't be keyed/fused; kept via BM25 fallback below
            vec = dense_mod.get_embedding(conn, pid)
            if vec is not None and len(vec) != qdim:
                vec = None  # dim mismatch (model changed) → no dense signal, degrade to BM25
            dense_scored.append((pid, _cosine(qvec, vec) if vec is not None else -1.0))
    finally:
        conn.close()
    dense_scored.sort(key=lambda x: -x[1])
    dense_ranking = [pid for pid, _ in dense_scored]
    bm25_ranking = [_pid(r) for r in bm25 if _pid(r)]

    fused = rrf_mod.rrf_fuse([bm25_ranking, dense_ranking], corpus_size=len(bm25))
    by_id = {_pid(r): r for r in bm25 if _pid(r)}
    merged = [by_id[pid] for pid, _ in fused if pid in by_id]
    # preserve any unidentifiable BM25 rows (no id) — append in original order, never drop
    for r in bm25:
        if not _pid(r):
            merged.append(r)

    merged = reranker_ar.rerank(ar_root, query, merged[:max(k, 15)], top_k=k)
    return merged[:k]
