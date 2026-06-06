"""RRF fusion helpers for AutoResearch hybrid search (Stage 1, opt-in module-ready).

Reuses search.py's dynamic RRF-k (`_compute_rrf_k`) and the SAME fusion formula it uses
inline (search.py:1066 → `1/(k + rank + 1)` per list, summed) so BM25 and dense rankings
combine identically to the main code search (PRD §12.2.8 — reuse, don't reimplement).
Pure functions: no deps, no I/O. Only the dense-active hybrid path calls this; the
default BM25-only path never does.
"""
from __future__ import annotations

from ..search import _compute_rrf_k

__all__ = ["compute_k", "rrf_fuse"]


def compute_k(corpus_size: int) -> int:
    """Dynamic RRF k (clamp 30..120, `AI_SEARCH_RRF_K` override) — reuses search.py."""
    return _compute_rrf_k(corpus_size)


def rrf_fuse(rankings: list[list[str]], corpus_size: int, k: int | None = None) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion over N best-first ranked id-lists.

    score(id) = Σ 1/(k + rank_in_list + 1) across lists where the id appears (identical to
    search.py:1066). Returns (id, score) pairs sorted by score descending.
    """
    if k is None:
        k = _compute_rrf_k(corpus_size)
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda kv: -kv[1])
