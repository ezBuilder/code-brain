"""Stage 0 smoke retrieval eval tests (PRD §12.3) — retrieval-miss regression guard."""
from __future__ import annotations

import math

from ai_core.autoresearch import evalset, fts, ingest, storage


def _build_corpus(ar):
    st = ingest.stage_source(ar, content="reciprocal rank fusion combines bm25 and dense retrieval; contextual retrieval prepends context")
    sid = st["source_id"]
    ingest.commit_pages(ar, source_id=sid, pages=[
        {"rel_path": "concepts/rrf.md", "content": "reciprocal rank fusion bm25 dense", "sources": [sid]},
        {"rel_path": "concepts/contextual.md", "content": "contextual retrieval prepends context to chunks", "sources": [sid]},
        {"rel_path": "entities/anthropic.md", "content": "anthropic contextual retrieval engineering", "sources": [sid]},
    ])


def test_smoke_eval_all_hit(tmp_path):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    _build_corpus(ar)
    golden = [
        {"query": "fusion", "expect": "concepts/rrf.md"},
        {"query": "contextual", "expect": "concepts/contextual.md"},
        {"query": "anthropic", "expect": "entities/anthropic.md"},
    ]
    rep = evalset.evaluate(ar, golden, k=5)
    assert rep["recall_at_k"] == 1.0 and rep["misses"] == [] and rep["hits"] == 3
    assert rep["hit_rate_at_k"] == 1.0 and rep["mrr"] == 1.0 and rep["ndcg_at_k"] == 1.0


def test_smoke_eval_detects_miss(tmp_path):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    _build_corpus(ar)
    rep = evalset.evaluate(ar, [{"query": "zzz nonexistent topic", "expect": "concepts/rrf.md"}], k=5)
    assert rep["recall_at_k"] == 0.0 and len(rep["misses"]) == 1
    assert rep["mrr"] == 0.0 and rep["ndcg_at_k"] == 0.0


def test_smoke_eval_reports_rank(tmp_path):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    _build_corpus(ar)
    rep = evalset.evaluate(ar, [{"query": "fusion", "expect": "concepts/rrf.md"}], k=5)
    assert rep["results"][0]["rank"] == 1


def test_fts_accepts_natural_language_punctuation(tmp_path):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    _build_corpus(ar)
    for query in (
        "How does reciprocal/fusion work?",
        "retrieval: contextual chunks",
        "dense-retrieval ranking",
    ):
        result = fts.search(ar, query, k=5)
        assert result and "error" not in result[0]


def test_smoke_eval_reports_rank_sensitive_metrics(tmp_path, monkeypatch):
    monkeypatch.setattr(
        evalset.fts_mod,
        "search",
        lambda _root, _query, k: [{"page": "noise.md"}, {"page": "target.md"}][:k],
    )
    rep = evalset.evaluate(tmp_path, [{"query": "topic", "expect": "target.md"}], k=5)
    assert rep["recall_at_k"] == 1.0
    assert rep["mrr"] == 0.5
    assert rep["ndcg_at_k"] == round(1.0 / math.log2(3), 6)


def test_smoke_eval_supports_multiple_relevant_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(
        evalset.fts_mod,
        "search",
        lambda _root, _query, k: [
            {"page": "a.md"},
            {"page": "noise.md"},
            {"page": "b.md"},
        ][:k],
    )
    rep = evalset.evaluate(tmp_path, [{"query": "topic", "expect": ["a.md", "b.md"]}], k=5)
    expected_ndcg = (1.0 + 1.0 / math.log2(4)) / (1.0 + 1.0 / math.log2(3))
    assert rep["recall_at_k"] == 1.0
    assert rep["mrr"] == 1.0
    assert rep["ndcg_at_k"] == round(expected_ndcg, 6)


def test_load_golden_tsv(tmp_path):
    p = tmp_path / "golden.tsv"
    p.write_text("# comment\nfusion\tconcepts/rrf.md\ncontextual\tconcepts/contextual.md\n\n", encoding="utf-8")
    golden = evalset.load_golden(p)
    assert golden == [
        {"query": "fusion", "expect": "concepts/rrf.md"},
        {"query": "contextual", "expect": "concepts/contextual.md"},
    ]


def test_load_golden_missing_file(tmp_path):
    assert evalset.load_golden(tmp_path / "nope.tsv") == []
