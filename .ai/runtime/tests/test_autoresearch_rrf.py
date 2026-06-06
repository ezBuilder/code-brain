"""RRF fusion tests (Stage 1 hybrid, opt-in) — formula matches search.py."""
from __future__ import annotations

from ai_core.autoresearch import rrf
from ai_core.search import _compute_rrf_k


def test_compute_k_reuses_search():
    for n in (16, 256, 1024, 100000):
        assert rrf.compute_k(n) == _compute_rrf_k(n)


def test_rrf_fuse_formula_matches_search():
    k = rrf.compute_k(16)
    fused = dict(rrf.rrf_fuse([["x", "y"]], corpus_size=16))
    assert abs(fused["x"] - 1.0 / (k + 0 + 1)) < 1e-9
    assert abs(fused["y"] - 1.0 / (k + 1 + 1)) < 1e-9


def test_rrf_fuse_combines_two_lists():
    # 'a' and 'b' appear in both rankings → outrank singletons 'c','d'
    fused = rrf.rrf_fuse([["a", "b", "c"], ["b", "a", "d"]], corpus_size=16)
    ids = [d for d, _ in fused]
    assert set(ids) == {"a", "b", "c", "d"}
    assert set(ids[:2]) == {"a", "b"}


def test_rrf_fuse_explicit_k_override():
    fused = dict(rrf.rrf_fuse([["a"]], corpus_size=16, k=10))
    assert abs(fused["a"] - 1.0 / (10 + 0 + 1)) < 1e-9


def test_rrf_fuse_empty():
    assert rrf.rrf_fuse([], corpus_size=16) == []
    assert rrf.rrf_fuse([[]], corpus_size=16) == []
