from __future__ import annotations

import math

from ai_core.code_retrieval_eval import canonical_result_path, corpus_snapshot_sha256
from ai_core.ranking_metrics import evaluate_ranked_retrieval, expected_ids


def test_ranked_metrics_are_macro_averaged_and_rank_sensitive() -> None:
    rankings = {
        "first": ["a.md", "noise.md", "b.md"],
        "second": ["noise.md", "target.md"],
    }
    report = evaluate_ranked_retrieval(
        [
            {"query": "first", "expect": ["a.md", "b.md"]},
            {"query": "second", "expect": "target.md"},
        ],
        lambda query, k: rankings[query][:k],
        k=3,
    )

    first_ndcg = (1.0 + 1.0 / math.log2(4)) / (1.0 + 1.0 / math.log2(3))
    assert report["recall_at_k"] == 1.0
    assert report["mrr"] == 0.75
    assert report["ndcg_at_k"] == round((first_ndcg + 1.0 / math.log2(3)) / 2.0, 6)
    assert report["latency_ms"]["p95"] >= report["latency_ms"]["p50"] >= 0.0


def test_expected_ids_deduplicates_and_ignores_empty_values() -> None:
    assert expected_ids(["a", "", "a", "b"]) == ["a", "b"]


def test_function_chunk_paths_collapse_to_source_file() -> None:
    assert canonical_result_path("src/app.py:Worker.run") == "src/app.py"
    assert canonical_result_path("src/app.ts::handler") == "src/app.ts"
    assert canonical_result_path("docs/design.md") == "docs/design.md"


def test_corpus_snapshot_hash_changes_with_source_content(tmp_path) -> None:
    source = tmp_path / "src" / "main.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    first = corpus_snapshot_sha256(tmp_path)
    source.write_text("VALUE = 2\n", encoding="utf-8")
    second = corpus_snapshot_sha256(tmp_path)
    assert first != second
    assert len(first) == len(second) == 64
