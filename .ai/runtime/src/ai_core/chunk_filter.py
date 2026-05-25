"""CODEFILTER-style chunk impact filter (PoC).

Reference: arXiv 2508.05970. Retrieved code chunks are not uniformly helpful;
some inject noise that degrades downstream LLM accuracy. This module assigns a
polarity (pos/neg/neu) per chunk using lightweight heuristics so callers can
drop negative chunks before sending them to a model.

This file is intentionally a pure module: no file I/O, no third-party deps,
standard library only. The Code Brain ``code_query`` MCP returns chunk dicts
shaped like ``{"path": str, "snippet": str, "lang"?: str, "rank"?: int,
"score"?: float}``; the same shape is consumed here.

A future revision can swap the heuristic ``score_chunk`` for a small learned
classifier without touching the public ``filter_chunks`` signature.
"""

from __future__ import annotations

import re
from typing import Any


# ---------------------------------------------------------------------------
# Heuristic tuning constants
# ---------------------------------------------------------------------------

# Symbol-like identifier extractor: camelCase / snake_case / PascalCase.
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_MIN_TOKEN_LEN = 3

# Snippet length bands.
_SHORT_THRESHOLD = 30
_LONG_THRESHOLD = 1500

# Comment-leading-line patterns. We check the *first non-whitespace* run.
_COMMENT_PREFIXES = ("#", "//", "/*", "*", "--", ";")

# Polarity thresholds on the [0, 1] score axis.
_POS_THRESHOLD = 0.5
_NEU_THRESHOLD = 0.3


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> set[str]:
    """Return the set of identifier-like tokens of length >= ``_MIN_TOKEN_LEN``.

    Case-insensitive: tokens are lower-cased so ``GetUser`` and ``get_user``
    overlap on ``get`` / ``user``. We additionally split CamelCase and
    snake_case so multi-part identifiers contribute their parts.
    """
    out: set[str] = set()
    for raw in _IDENT_RE.findall(text or ""):
        for piece in _split_identifier(raw):
            if len(piece) >= _MIN_TOKEN_LEN:
                out.add(piece.lower())
    return out


def _split_identifier(ident: str) -> list[str]:
    """Split ``getUserBalance`` -> [getUserBalance, get, User, Balance].

    Also splits on underscores. The full original identifier is kept so an
    exact match (e.g. user query has ``getUserBalance`` verbatim) counts.
    """
    parts: list[str] = [ident]
    # snake_case split
    snake_pieces = [p for p in ident.split("_") if p]
    if len(snake_pieces) > 1:
        parts.extend(snake_pieces)
    # camelCase / PascalCase split on each snake piece
    for piece in list(parts):
        camel = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+", piece)
        if len(camel) > 1:
            parts.extend(camel)
    return parts


def _is_comment_only(snippet: str) -> bool:
    """Return True if a majority of non-empty lines are comment-leading."""
    lines = [ln.strip() for ln in snippet.splitlines() if ln.strip()]
    if not lines:
        return False
    comment_lines = sum(1 for ln in lines if ln.startswith(_COMMENT_PREFIXES))
    return comment_lines * 2 > len(lines)  # strict majority


def _query_substring_hits(query: str, snippet: str) -> int:
    """Count how many whole query words appear verbatim in the snippet.

    Case-insensitive whole-word match — guards against ``get`` matching
    ``budget``.
    """
    q_words = [w for w in _IDENT_RE.findall(query or "") if len(w) >= _MIN_TOKEN_LEN]
    if not q_words:
        return 0
    haystack = snippet or ""
    hits = 0
    for w in q_words:
        pattern = re.compile(rf"\b{re.escape(w)}\b", re.IGNORECASE)
        if pattern.search(haystack):
            hits += 1
    return hits


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def score_chunk(query: str, chunk: dict) -> dict:
    """Score a single chunk's polarity relative to ``query``.

    Returns ``{"polarity": "pos"|"neg"|"neu", "score": float, "reasons": [...]}``.
    """
    snippet = str(chunk.get("snippet") or "")
    reasons: list[str] = []

    q_tokens = _tokenize(query or "")
    s_tokens = _tokenize(snippet)
    shared = q_tokens & s_tokens
    shared_count = len(shared)
    reasons.append(f"shared_ids={shared_count}")

    # Base signal: token overlap normalized by query token count.
    # If the query has 0 usable tokens, fall back to neutral baseline.
    if q_tokens:
        overlap_ratio = shared_count / len(q_tokens)
    else:
        overlap_ratio = 0.0
    score = min(1.0, overlap_ratio)

    # Bonus: verbatim word hit (independent signal, e.g. natural-language query
    # that has rare nouns in the snippet).
    sub_hits = _query_substring_hits(query, snippet)
    if sub_hits:
        reasons.append(f"query_term_present={sub_hits}")
        score = min(1.0, score + 0.15 * sub_hits)

    # Penalties
    snip_len = len(snippet)
    if snip_len < _SHORT_THRESHOLD:
        reasons.append("snippet_too_short")
        score -= 0.4
    elif snip_len > _LONG_THRESHOLD:
        reasons.append("snippet_too_long")
        score -= 0.2

    if _is_comment_only(snippet):
        reasons.append("comment_only")
        # Comment-only chunks are explanatory noise; cap them out of 'pos'.
        score = min(score, _POS_THRESHOLD - 0.05) - 0.15
        if score < 0.0:
            score = 0.0

    # Clamp.
    if score < 0.0:
        score = 0.0
    elif score > 1.0:
        score = 1.0

    if score >= _POS_THRESHOLD:
        polarity = "pos"
    elif score >= _NEU_THRESHOLD:
        polarity = "neu"
    else:
        polarity = "neg"

    return {"polarity": polarity, "score": round(score, 4), "reasons": reasons}


def filter_chunks(
    query: str,
    chunks: list[dict],
    *,
    drop_negatives: bool = True,
    max_keep: int | None = None,
) -> dict:
    """Apply CODEFILTER-style polarity filtering to a chunk list.

    Returns::

        {
          "ok": bool,
          "kept": [chunk + {cf_score, cf_polarity}, ...],
          "dropped": [{"chunk": <orig>, "reason": "neg"|"truncated"}, ...],
          "summary": {"pos": int, "neg": int, "neu": int},
        }
    """
    summary = {"pos": 0, "neg": 0, "neu": 0}
    kept: list[dict] = []
    dropped: list[dict[str, Any]] = []

    if not chunks:
        return {"ok": True, "kept": [], "dropped": [], "summary": summary}

    scored: list[dict] = []
    for ch in chunks:
        if not isinstance(ch, dict):
            continue
        info = score_chunk(query or "", ch)
        summary[info["polarity"]] += 1
        enriched = dict(ch)
        enriched["cf_score"] = info["score"]
        enriched["cf_polarity"] = info["polarity"]
        enriched["cf_reasons"] = info["reasons"]
        scored.append(enriched)

    # Drop negatives if requested.
    for ch in scored:
        if drop_negatives and ch["cf_polarity"] == "neg":
            dropped.append({"chunk": ch, "reason": "neg"})
        else:
            kept.append(ch)

    # Sort by score descending (stable for ties to preserve original order).
    kept.sort(key=lambda c: c["cf_score"], reverse=True)

    # Truncate to max_keep.
    if max_keep is not None and max_keep >= 0 and len(kept) > max_keep:
        overflow = kept[max_keep:]
        kept = kept[:max_keep]
        for ch in overflow:
            dropped.append({"chunk": ch, "reason": "truncated"})

    return {"ok": True, "kept": kept, "dropped": dropped, "summary": summary}


__all__ = ["score_chunk", "filter_chunks"]
