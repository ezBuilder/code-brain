"""Deterministic citation verification (Stage 3 autoresearch_verify, NO LLM).

Scores each claim's quote against its cited source texts via verify_matcher (graded
faithfulness). The LLM-as-judge *factuality* stage (is it true in the world?) is the
calling agent's job; this returns the deterministic *faithfulness* signal (is it actually
in the source?) that the agent uses to accept / hedge / reject. Reuses manifest (source
existence), storage (raw text), verify_matcher (graded match). stdlib only.
"""
from __future__ import annotations

from pathlib import Path

from . import storage, verify_matcher
from . import manifest as manifest_mod


def _source_text(ar_root: Path, source_id: str) -> str | None:
    p = storage.raw_dir(ar_root) / f"{source_id}.txt"
    return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else None


def verify_claims(ar_root: Path, claims: list[dict], *, long_tail_ids: list[str] | None = None) -> dict:
    """claims: [{quote, sources:[ids]}]. Returns per-claim faithfulness + corpus overall.

    Each claim is scored against the BEST-matching cited source. Missing/unreadable sources
    score 0. long_tail_ids get exact-only matching (no fuzzy) per reference-hallucination findings.
    """
    long_tail = set(long_tail_ids or [])
    results = []
    for c in claims:
        quote = str(c.get("quote", ""))
        sources = list(c.get("sources") or [])
        best = {"score": 0.0, "kind": "no_source"}
        matched = None
        for sid in sources:
            if not manifest_mod.id_exists(ar_root, sid):
                continue
            txt = _source_text(ar_root, sid)
            if txt is None:
                continue
            sc = verify_matcher.match_score(quote, txt, long_tail=(sid in long_tail))
            if sc["score"] > best["score"]:
                best, matched = sc, sid
        results.append({
            "quote": quote,
            "sources": sources,
            "matched_source": matched,
            "faithfulness": best["score"],
            "kind": best["kind"],
        })
    overall = round(sum(r["faithfulness"] for r in results) / len(results), 3) if results else 0.0
    return {"claims": results, "overall_faithfulness": overall, "count": len(results)}
