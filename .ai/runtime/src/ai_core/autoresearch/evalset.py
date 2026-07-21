"""Stage 0 smoke retrieval eval (PRD §12.3 / §3.5).

Lightweight retrieval-miss regression: a fixed set of (query → expected page) pairs,
checking the expected page appears in top-k. This is NOT formal NDCG/MRR — that arrives
in Stage 1 with a held-out set. It is a cheap guard that indexing/search changes don't
silently regress retrieval. stdlib only.
"""
from __future__ import annotations

from pathlib import Path

from . import fts as fts_mod


def evaluate(ar_root: Path, golden: list[dict], k: int = 5) -> dict:
    """golden: [{"query": str, "expect": rel_path}]. Returns recall@k and per-query detail."""
    results: list[dict] = []
    hits = 0
    for g in golden:
        q = str(g.get("query", ""))
        expect = str(g.get("expect", ""))
        found = fts_mod.search(ar_root, q, k=k)
        pages = [h.get("page") for h in found if isinstance(h, dict) and "error" not in h]
        hit = expect in pages
        results.append({
            "query": q,
            "expect": expect,
            "hit": hit,
            "rank": (pages.index(expect) + 1) if hit else None,
        })
        hits += 1 if hit else 0
    total = len(golden)
    return {
        "recall_at_k": round(hits / total, 4) if total else 0.0,
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
