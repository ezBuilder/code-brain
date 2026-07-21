"""Stage 0 smoke retrieval eval (PRD §12.3 / §3.5).

Lightweight retrieval-miss regression: a fixed set of (query → expected page) pairs,
checking expected pages appear in top-k. It reports Recall@K, MRR, and binary NDCG@K,
but remains a small smoke set rather than the formal 30–50 query held-out benchmark
required before Stage 1. It is a cheap guard that indexing/search changes do not silently
regress retrieval. stdlib only.
"""
from __future__ import annotations

from pathlib import Path

from ..ranking_metrics import evaluate_ranked_retrieval
from . import fts as fts_mod


def evaluate(ar_root: Path, golden: list[dict], k: int = 5) -> dict:
    """Evaluate ranked retrieval against binary relevance labels.

    ``golden`` accepts ``expect`` as either one relative page path or a list of
    relevant paths. Metrics are macro-averaged per query so one topic with many
    labels cannot dominate the smoke suite.
    """
    def ranked_search(query_text: str, requested_k: int) -> list[str]:
        found = fts_mod.search(ar_root, query_text, k=requested_k)
        return [
            str(item.get("page"))
            for item in found
            if isinstance(item, dict) and "error" not in item and item.get("page")
        ]

    return evaluate_ranked_retrieval(golden, ranked_search, k=k)


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
