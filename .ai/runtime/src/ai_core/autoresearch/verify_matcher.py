"""Deterministic graded citation matcher for AutoResearch (Stage 3 verify, NO LLM).

Extends verify_det's binary substring check into a faithfulness *score* in [0,1]:
exact substring (1.0), fuzzy small-typo (0.7), paraphrase-ish (0.4), or miss (0.0).
Long-tail entities (rare in the corpus) get exact-only — no fuzzy — because reference
hallucination concentrates there. Pure functions; stdlib only (re, difflib). Inputs are
length-capped to bound SequenceMatcher cost (DoS guard).
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

_WS = re.compile(r"\s+")
_MAX_LEN = 100_000  # cap inputs to bound O(n^2) matching cost


def normalize(text: str) -> str:
    """Whitespace-collapse + lowercase (matches verify_det's normalization)."""
    return _WS.sub(" ", text).strip().lower()


def _best_window_ratio(quote_n: str, source_n: str) -> float:
    """Best fuzzy ratio of the quote against any quote-length window of source."""
    if not quote_n:
        return 1.0
    if quote_n in source_n:
        return 1.0
    n = len(quote_n)
    if len(source_n) < n:
        return SequenceMatcher(None, quote_n, source_n).ratio()
    step = max(1, n // 4)
    best = 0.0
    for i in range(0, len(source_n) - n + 1, step):
        r = SequenceMatcher(None, quote_n, source_n[i:i + n]).ratio()
        if r > best:
            best = r
            if best >= 0.995:
                break
    return best


def match_score(quote: str, source_text: str, *, long_tail: bool = False) -> dict:
    """Graded faithfulness of `quote` against `source_text`.

    exact substring → 1.0; fuzzy ratio ≥0.9 → 0.7 (typos); ≥0.75 → 0.4 (paraphrase);
    else 0.0. long_tail=True disables fuzzy (exact-only) — rare entities must match exactly.
    """
    qn = normalize(quote[:_MAX_LEN])
    sn = normalize(source_text[:_MAX_LEN])
    if not qn:
        return {"score": 1.0, "kind": "empty", "exact": True}
    if qn in sn:
        return {"score": 1.0, "kind": "exact", "exact": True}
    if long_tail:
        return {"score": 0.0, "kind": "long_tail_miss", "exact": False}
    ratio = _best_window_ratio(qn, sn)
    if ratio >= 0.9:
        return {"score": 0.7, "kind": "fuzzy", "exact": False, "ratio": round(ratio, 3)}
    if ratio >= 0.75:
        return {"score": 0.4, "kind": "paraphrase", "exact": False, "ratio": round(ratio, 3)}
    return {"score": 0.0, "kind": "miss", "exact": False, "ratio": round(ratio, 3)}
