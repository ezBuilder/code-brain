"""AutoResearch reranker wrapper tests (Stage 1, opt-in) — no-op preserves order without deps."""
from __future__ import annotations

from ai_core.autoresearch import reranker_ar, storage


def test_is_active_for_no_deps(tmp_path):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    # no ONNX deps in this env → inactive
    assert reranker_ar.is_active_for(ar) is False


def test_rerank_noop_preserves_candidates(tmp_path):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    cands = [{"page": "a.md", "snippet": "alpha", "score": 1.0},
             {"page": "b.md", "snippet": "beta", "score": 2.0}]
    out = reranker_ar.rerank(ar, "query", cands)
    assert out == cands  # unchanged when inactive (BM25/RRF order preserved)


def test_rerank_empty(tmp_path):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    assert reranker_ar.rerank(ar, "q", []) == []
