"""Cross-encoder reranker for AutoResearch hybrid search (Stage 1, opt-in, no-deps default).

Thin wrapper over reranker.py (Xenova/ms-marco-MiniLM, AI_SEARCH_RERANK). Reranks the
post-RRF shortlist ONLY — never the full index (PRD §4.3: reranking lifts MAP but is heavy,
so keep N small). No-op (candidates unchanged) when deps/model absent or rerank disabled.
Reuse only; candidates carry a 'snippet' field which the cross-encoder scores.
"""
from __future__ import annotations

from pathlib import Path

from .. import reranker as _rr


def _project_root(ar_root: Path) -> Path:
    return ar_root.parent.parent


def is_active_for(ar_root: Path) -> bool:
    return _rr.is_active_for(_project_root(ar_root))


def rerank(ar_root: Path, query: str, candidates: list[dict], top_k: int | None = None) -> list[dict]:
    """Rerank a shortlist via cross-encoder; returns candidates UNCHANGED when inactive
    (no-op preserves the BM25/RRF order). Only the passed shortlist is scored."""
    if not candidates:
        return candidates
    reranked = _rr.rerank(query, candidates, _project_root(ar_root), top_k=top_k)
    return reranked if reranked is not None else candidates
