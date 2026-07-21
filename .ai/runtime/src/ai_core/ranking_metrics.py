"""Deterministic ranked-retrieval metrics shared by Code Brain eval axes."""
from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any

RankedSearch = Callable[[str, int], list[str]]


def expected_ids(value: object) -> list[str]:
    raw = value if isinstance(value, list) else [value]
    return list(dict.fromkeys(str(item).strip() for item in raw if str(item).strip()))


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil((max(0.0, min(100.0, percentile)) / 100.0) * len(ordered)))
    return ordered[min(len(ordered), rank) - 1]


def evaluate_ranked_retrieval(
    golden: list[dict[str, Any]],
    search: RankedSearch,
    *,
    k: int = 5,
) -> dict[str, Any]:
    """Macro-average Recall@K, MRR and binary NDCG@K with latency evidence."""
    bounded_k = max(1, int(k))
    results: list[dict[str, Any]] = []
    durations: list[float] = []
    hits = 0
    recall_total = 0.0
    reciprocal_rank_total = 0.0
    ndcg_total = 0.0

    for item in golden:
        query = str(item.get("query") or "")
        expected = expected_ids(item.get("expect"))
        started = time.perf_counter_ns()
        ranked = search(query, bounded_k)
        duration_ms = round((time.perf_counter_ns() - started) / 1_000_000, 3)
        durations.append(duration_ms)
        pages = list(dict.fromkeys(str(page) for page in ranked if str(page)))[:bounded_k]
        ranks = sorted(pages.index(page) + 1 for page in expected if page in pages)
        relevant_retrieved = len(ranks)
        query_recall = relevant_retrieved / len(expected) if expected else 0.0
        reciprocal_rank = 1.0 / ranks[0] if ranks else 0.0
        dcg = sum(1.0 / math.log2(rank + 1) for rank in ranks)
        ideal_count = min(len(expected), bounded_k)
        idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_count + 1))
        ndcg = dcg / idcg if idcg else 0.0
        hit = bool(ranks)
        result = {
            "query": query,
            "expect": item.get("expect"),
            "expected_pages": expected,
            "retrieved_pages": pages,
            "hit": hit,
            "rank": ranks[0] if ranks else None,
            "relevant_retrieved": relevant_retrieved,
            "recall_at_k": round(query_recall, 6),
            "reciprocal_rank": round(reciprocal_rank, 6),
            "ndcg_at_k": round(ndcg, 6),
            "duration_ms": duration_ms,
        }
        results.append(result)
        hits += int(hit)
        recall_total += query_recall
        reciprocal_rank_total += reciprocal_rank
        ndcg_total += ndcg

    total = len(golden)
    return {
        "recall_at_k": round(recall_total / total, 6) if total else 0.0,
        "hit_rate_at_k": round(hits / total, 6) if total else 0.0,
        "mrr": round(reciprocal_rank_total / total, 6) if total else 0.0,
        "ndcg_at_k": round(ndcg_total / total, 6) if total else 0.0,
        "latency_ms": {
            "p50": round(_percentile(durations, 50.0), 3),
            "p95": round(_percentile(durations, 95.0), 3),
            "max": round(max(durations), 3) if durations else 0.0,
        },
        "k": bounded_k,
        "total": total,
        "hits": hits,
        "misses": [result for result in results if not result["hit"]],
        "results": results,
    }


__all__ = ["evaluate_ranked_retrieval", "expected_ids"]
