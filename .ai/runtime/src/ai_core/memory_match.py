"""Unicode-aware deterministic text matching for durable memory retrieval."""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any

_CAMEL_ACRONYM = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_CAMEL_LOWER = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_TOKEN_RE = re.compile(r"[^\W_]+", flags=re.UNICODE)


def tokenize(value: object) -> list[str]:
    """Split paths, snake/kebab names, camelCase and Unicode text consistently."""
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    separated = _CAMEL_ACRONYM.sub(" ", _CAMEL_LOWER.sub(" ", normalized))
    return list(dict.fromkeys(token.casefold() for token in _TOKEN_RE.findall(separated) if token))


def compact(value: object) -> str:
    """Separator-insensitive normalized form for exact phrase/entity matching."""
    return "".join(tokenize(value))


def token_similarity(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _token_strength(needle: str, candidates: set[str]) -> float:
    if needle in candidates:
        return 1.0
    if len(needle) < 3:
        return 0.0
    for candidate in candidates:
        if len(candidate) < 3:
            continue
        if needle in candidate or candidate in needle:
            return 0.72
    return 0.0


def weighted_relevance(
    query_tokens: list[str],
    fields: Mapping[str, tuple[object, float]],
    *,
    query_text: object = "",
) -> tuple[float, dict[str, list[str]]]:
    """Return field-aware token coverage and explain which fields matched.

    Weights influence which field supplies the best match for a query token,
    while the final relevance remains bounded to [0, 1]. Exact compact phrase
    matches receive a small bonus so identifiers and paths rank predictably.
    """
    unique_query = list(dict.fromkeys(query_tokens))
    if not unique_query:
        return 0.0, {}
    token_fields: dict[str, tuple[set[str], float, str]] = {}
    for name, (value, raw_weight) in fields.items():
        weight = max(0.0, min(1.25, float(raw_weight)))
        token_fields[str(name)] = (set(tokenize(value)), weight, compact(value))

    matched_fields: dict[str, list[str]] = {}
    total = 0.0
    for token in unique_query:
        best = 0.0
        best_field: str | None = None
        for field_name, (candidates, weight, _compact_value) in token_fields.items():
            strength = _token_strength(token, candidates) * weight
            if strength > best:
                best = strength
                best_field = field_name
        total += min(1.0, best)
        if best_field is not None and best > 0.0:
            matched_fields.setdefault(best_field, []).append(token)

    relevance = total / len(unique_query)
    compact_query = compact(query_text)
    if len(compact_query) >= 4:
        phrase_weight = max(
            (
                min(1.0, weight)
                for _name, (_tokens, weight, compact_value) in token_fields.items()
                if compact_query and compact_query in compact_value
            ),
            default=0.0,
        )
        relevance += 0.08 * phrase_weight
    return min(1.0, relevance), matched_fields


def stable_text_key(value: object) -> tuple[str, set[str]]:
    terms = set(tokenize(value))
    return " ".join(sorted(terms)), terms


def flatten_evidence(value: Any, *, max_items: int = 32) -> str:
    """Render bounded scalar provenance fields for local matching only."""
    parts: list[str] = []

    def visit(item: Any) -> None:
        if len(parts) >= max_items:
            return
        if isinstance(item, Mapping):
            for key, child in list(item.items())[:max_items]:
                parts.append(str(key))
                visit(child)
                if len(parts) >= max_items:
                    break
        elif isinstance(item, (list, tuple, set)):
            for child in list(item)[:max_items]:
                visit(child)
                if len(parts) >= max_items:
                    break
        elif item is not None:
            parts.append(str(item))

    visit(value)
    return " ".join(parts[:max_items])


__all__ = [
    "compact",
    "flatten_evidence",
    "stable_text_key",
    "token_similarity",
    "tokenize",
    "weighted_relevance",
]
