"""Stage 0 smoke retrieval eval (PRD §12.3 / §3.5).

Lightweight retrieval-miss regression: a fixed set of (query → expected page) pairs,
checking expected pages appear in top-k. It reports Recall@K, MRR, and binary NDCG@K,
but remains a small smoke set rather than the formal 30–50 query held-out benchmark
required before Stage 1. It is a cheap guard that indexing/search changes do not silently
regress retrieval. stdlib only.
"""
from __future__ import annotations

import math
from pathlib import Path

from . import fts as fts_mod


def _expected_pages(value: object) -> list[str]:
    raw = value if isinstance(value, list) else [value]
    return list(dict.fromkeys(str(item).strip() for item in raw if str(item).strip()))


def evaluate(ar_root: Path, golden: list[dict], k: int = 5) -> dict:
    """Evaluate ranked retrieval against binary relevance labels.

    ``golden`` accepts ``expect`` as either one relative page path or a list of
    relevant paths. Metrics are macro-averaged per query so one topic with many
    labels cannot dominate the smoke suite.
    """
    k = max(1, int(k))
    results: list[dict] = []
    hits = 0
    recall_total = 0.0
    reciprocal_rank_total = 0.0
    ndcg_total = 0.0
    for g in golden:
        q = str(g.get("query", ""))
        expected = _expected_pages(g.get("expect"))
        found = fts_mod.search(ar_root, q, k=k)
        pages = [str(h.get("page")) for h in found if isinstance(h, dict) and "error" not in h and h.get("page")]
        ranks = sorted(pages.index(page) + 1 for page in expected if page in pages)
        relevant_retrieved = len(ranks)
        query_recall = relevant_retrieved / len(expected) if expected else 0.0
        reciprocal_rank = 1.0 / ranks[0] if ranks else 0.0
        dcg = sum(1.0 / math.log2(rank + 1) for rank in ranks)
        ideal_count = min(len(expected), k)
        idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_count + 1))
        ndcg = dcg / idcg if idcg else 0.0
        hit = bool(ranks)
        results.append({
            "query": q,
            "expect": g.get("expect"),
            "expected_pages": expected,
            "hit": hit,
            "rank": ranks[0] if ranks else None,
            "relevant_retrieved": relevant_retrieved,
            "recall_at_k": round(query_recall, 6),
            "reciprocal_rank": round(reciprocal_rank, 6),
            "ndcg_at_k": round(ndcg, 6),
        })
        hits += 1 if hit else 0
        recall_total += query_recall
        reciprocal_rank_total += reciprocal_rank
        ndcg_total += ndcg
    total = len(golden)
    return {
        "recall_at_k": round(recall_total / total, 6) if total else 0.0,
        "hit_rate_at_k": round(hits / total, 6) if total else 0.0,
        "mrr": round(reciprocal_rank_total / total, 6) if total else 0.0,
        "ndcg_at_k": round(ndcg_total / total, 6) if total else 0.0,
        "k": k,
        "total": total,
        "hits": hits,
        "misses": [r for r in results if not r["hit"]],
        "results": results,
    }


def load_golden(path: Path) -> list[dict]:
    """Load golden pairs from a TSV: `query<TAB>expected_rel_path` per line (# comments ok)."""
    out: list[dict] = []
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 2:
            out.append({"query": parts[0], "expect": parts[1]})
    return out
