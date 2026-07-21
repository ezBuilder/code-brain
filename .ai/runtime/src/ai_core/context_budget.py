from __future__ import annotations

import hashlib
from typing import Any

from .memory_match import compact, flatten_evidence, token_similarity, tokenize

MODES = ("high_fidelity", "balanced", "aggressive")
PROTECTED_SIGNALS = ("handoff", "rubric", "verdict", "blockers")
PROTECTED_PROVENANCE_KEYS = (
    "citation",
    "document_id",
    "evidence_id",
    "record_id",
    "sha256",
    "source",
    "source_id",
    "url",
)
NEGATIVE_EVIDENCE_SIGNALS = (
    "failure",
    "failed",
    "error",
    "exception",
    "blocked",
    "regression",
    "counterexample",
    "negative evidence",
    "not supported",
    "does not work",
)
DEFAULT_MODE = "balanced"
DEFAULT_MAX_BYTES = 4096
MAX_CANDIDATES = 256
CANDIDATE_MULTIPLIER = 4
MODE_BYTE_MULTIPLIERS = {
    "high_fidelity": 2.0,
    "balanced": 1.0,
    "aggressive": 0.5,
}
MODE_RESULT_LIMITS = {
    "high_fidelity": None,
    "balanced": None,
    "aggressive": 3,
}
MODE_DUPLICATE_THRESHOLDS = {
    "high_fidelity": 0.96,
    "balanced": 0.84,
    "aggressive": 0.72,
}


def normalize_mode(mode: str | None) -> str:
    normalized = (mode or DEFAULT_MODE).strip().lower().replace("-", "_")
    if normalized not in MODES:
        raise ValueError(f"invalid context budget mode: {mode}")
    return normalized


def policy(mode: str | None = None, *, base_max_bytes: int = DEFAULT_MAX_BYTES) -> dict[str, Any]:
    normalized = normalize_mode(mode)
    base = max(512, int(base_max_bytes or DEFAULT_MAX_BYTES))
    max_bytes = int(base * MODE_BYTE_MULTIPLIERS[normalized])
    return {
        "mode": normalized,
        "max_bytes": max(512, max_bytes),
        "max_results": MODE_RESULT_LIMITS[normalized],
        "protected_signals": list(PROTECTED_SIGNALS),
        "protected_provenance_keys": list(PROTECTED_PROVENANCE_KEYS),
        "negative_evidence_signals": list(NEGATIVE_EVIDENCE_SIGNALS),
        "duplicate_similarity": MODE_DUPLICATE_THRESHOLDS[normalized],
        "candidate_multiplier": CANDIDATE_MULTIPLIER,
        "candidate_max": MAX_CANDIDATES,
    }


def candidate_limit(limit: int) -> int:
    requested = max(1, int(limit))
    return min(MAX_CANDIDATES, max(requested, requested * CANDIDATE_MULTIPLIER))


def _text(item: dict[str, Any]) -> str:
    return (
        f"{item.get('path', '')}\n"
        f"{item.get('scope', '')}\n"
        f"{item.get('snippet', '')}\n"
        f"{flatten_evidence(item.get('provenance'))}"
    )


def _has_protected_signal(item: dict[str, Any]) -> bool:
    haystack = _text(item).casefold()
    return any(signal in haystack for signal in PROTECTED_SIGNALS)


def _has_negative_evidence(item: dict[str, Any]) -> bool:
    haystack = _text(item)
    terms = set(tokenize(haystack))
    compact_haystack = compact(haystack)
    for signal in NEGATIVE_EVIDENCE_SIGNALS:
        signal_terms = tokenize(signal)
        if len(signal_terms) == 1 and signal_terms[0] in terms:
            return True
        if len(signal_terms) > 1 and compact(signal) in compact_haystack:
            return True
    status = str(item.get("status") or item.get("outcome") or "").casefold()
    return status in {"failure", "failed", "error", "exception", "blocked", "refuted", "stale"}


def _has_protected_provenance(item: dict[str, Any]) -> bool:
    provenance = item.get("provenance")
    if not isinstance(provenance, dict):
        return False
    for key in PROTECTED_PROVENANCE_KEYS:
        value = provenance.get(key)
        if value is not None and value != "" and value != [] and value != {}:
            return True
    return False


def _is_preserved(item: dict[str, Any]) -> bool:
    return (
        _has_protected_signal(item)
        or _has_negative_evidence(item)
        or _has_protected_provenance(item)
    )


def _line(item: dict[str, Any]) -> str:
    # Fail-soft: tolerate malformed items in hook-wrapped paths.
    return f"- {item.get('path', '')}: {item.get('snippet', '')}"


def _bytes(text: str) -> int:
    return len(text.encode("utf-8"))


def _item_tokens(item: dict[str, Any]) -> set[str]:
    return set(tokenize(_text(item)))


def _content_tokens(item: dict[str, Any]) -> set[str]:
    return set(tokenize(f"{item.get('scope', '')} {item.get('snippet', '')}"))


def _provenance_present(item: dict[str, Any]) -> bool:
    provenance = item.get("provenance")
    return isinstance(provenance, dict) and bool(provenance)


def _query_coverage(item: dict[str, Any], query_tokens: set[str]) -> float:
    if not query_tokens:
        return 0.0
    terms = _item_tokens(item)
    return len(query_tokens & terms) / len(query_tokens)


def _canonical_sort_key(
    item: dict[str, Any],
    *,
    query_tokens: set[str],
) -> tuple[int, int, int, float, int, str, str, str]:
    # Content-derived ordering remains invariant to volatile incoming rank.
    # Explicit handoff/rubric/verdict/blockers lead, then negative evidence,
    # followed by query coverage and provenance-bearing candidates.
    return (
        0 if _has_protected_signal(item) else 1,
        0 if _has_negative_evidence(item) else 1,
        0 if _has_protected_provenance(item) else 1,
        -_query_coverage(item, query_tokens),
        0 if _provenance_present(item) else 1,
        str(item.get("path", "")),
        str(item.get("scope", "")),
        str(item.get("snippet", "")),
    )


def _content_fingerprint(item: dict[str, Any]) -> str:
    normalized = " ".join(tokenize(f"{item.get('scope', '')} {item.get('snippet', '')}"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else ""


def _is_redundant(
    item: dict[str, Any],
    selected: list[dict[str, Any]],
    *,
    threshold: float,
) -> bool:
    fingerprint = _content_fingerprint(item)
    terms = _content_tokens(item)
    item_negative = _has_negative_evidence(item)
    for existing in selected:
        # A success/corroborating result and a failure/counterexample can share
        # most tokens while carrying opposite operational meaning. Preserve
        # both polarities instead of treating one as redundant.
        if item_negative != _has_negative_evidence(existing):
            continue
        if fingerprint and fingerprint == _content_fingerprint(existing):
            return True
        existing_terms = _content_tokens(existing)
        # Tiny snippets are too collision-prone for fuzzy suppression.
        if min(len(terms), len(existing_terms)) < 4:
            continue
        if token_similarity(terms, existing_terms) >= threshold:
            return True
    return False


def _marginal_sort_key(
    item: dict[str, Any],
    *,
    query_tokens: set[str],
    covered_terms: set[str],
) -> tuple[int, float, int, str, str, str]:
    item_query_terms = query_tokens & _item_tokens(item)
    marginal_terms = item_query_terms - covered_terms
    return (
        -len(marginal_terms),
        -_query_coverage(item, query_tokens),
        0 if _provenance_present(item) else 1,
        str(item.get("path", "")),
        str(item.get("scope", "")),
        str(item.get("snippet", "")),
    )


def _fit_items(
    items: list[dict[str, Any]],
    max_bytes: int,
) -> tuple[list[dict[str, Any]], int, bool]:
    if _bytes("\n".join(_line(item) for item in items)) <= max_bytes:
        return list(items), 0, False
    kept = list(items)
    dropped = 0
    for idx in range(len(kept) - 1, -1, -1):
        if _is_preserved(kept[idx]):
            continue
        kept.pop(idx)
        dropped += 1
        if _bytes("\n".join(_line(item) for item in kept)) <= max_bytes:
            return kept, dropped, False
    return kept, dropped, _bytes("\n".join(_line(item) for item in kept)) > max_bytes


def apply(
    results: list[dict[str, Any]],
    *,
    mode: str,
    limit: int,
    base_max_bytes: int = DEFAULT_MAX_BYTES,
    query: str | None = None,
) -> dict[str, Any]:
    from .retrieval_observation import build as build_retrieval_observation
    from .retrieval_observation import start as start_retrieval_observation

    started_ns = start_retrieval_observation()
    mode = normalize_mode(mode)
    budget = policy(mode, base_max_bytes=base_max_bytes)
    requested_limit = max(1, int(limit))
    query_tokens = set(tokenize(query or ""))
    valid_candidates = [item for item in results if isinstance(item, dict)]
    valid_candidates.sort(key=lambda item: _canonical_sort_key(item, query_tokens=query_tokens))
    scan_limit = candidate_limit(requested_limit)
    candidates = valid_candidates[:scan_limit]
    candidate_cap_pruned = max(0, len(valid_candidates) - len(candidates))

    selected = [item for item in candidates if _is_preserved(item)]
    selected.sort(key=lambda item: _canonical_sort_key(item, query_tokens=query_tokens))
    covered_during_selection: set[str] = set()
    for item in selected:
        covered_during_selection.update(query_tokens & _item_tokens(item))
    ordinary_candidates = [item for item in candidates if not _is_preserved(item)]
    ordinary_selected = 0
    redundancy_pruned = 0
    result_cap_pruned = 0
    configured_result_cap = budget["max_results"]
    max_ordinary = (
        min(requested_limit, int(configured_result_cap))
        if configured_result_cap is not None
        else requested_limit
    )
    duplicate_threshold = float(budget["duplicate_similarity"])

    while ordinary_candidates:
        ordinary_candidates.sort(
            key=lambda item: _marginal_sort_key(
                item,
                query_tokens=query_tokens,
                covered_terms=covered_during_selection,
            )
        )
        item = ordinary_candidates.pop(0)
        if _is_redundant(item, selected, threshold=duplicate_threshold):
            redundancy_pruned += 1
            continue
        if ordinary_selected >= max_ordinary:
            result_cap_pruned += 1
            continue
        selected.append(item)
        ordinary_selected += 1
        covered_during_selection.update(query_tokens & _item_tokens(item))

    before_byte_fit = list(selected)
    selected, byte_pruned, over_budget_to_preserve = _fit_items(
        selected,
        int(budget["max_bytes"]),
    )
    additional = "\n".join(_line(item) for item in selected)

    candidate_context = "\n".join(_line(item) for item in candidates)
    input_bytes = _bytes(candidate_context)
    output_bytes = _bytes(additional)
    saved_bytes = max(0, input_bytes - output_bytes)
    input_lexical_tokens = len(tokenize(candidate_context))
    output_lexical_tokens = len(tokenize(additional))
    saved_lexical_tokens = max(0, input_lexical_tokens - output_lexical_tokens)
    available_terms: set[str] = set()
    for item in candidates:
        available_terms.update(query_tokens & _item_tokens(item))
    covered_terms: set[str] = set()
    for item in selected:
        covered_terms.update(query_tokens & _item_tokens(item))

    protected_available = sum(1 for item in candidates if _has_protected_signal(item))
    protected_selected = sum(1 for item in selected if _has_protected_signal(item))
    negative_available = sum(1 for item in candidates if _has_negative_evidence(item))
    negative_selected = sum(1 for item in selected if _has_negative_evidence(item))
    protected_provenance_available = sum(1 for item in candidates if _has_protected_provenance(item))
    protected_provenance_selected = sum(1 for item in selected if _has_protected_provenance(item))
    provenance_available = sum(1 for item in candidates if _provenance_present(item))
    provenance_selected = sum(1 for item in selected if _provenance_present(item))
    lost_query_terms = sorted(available_terms - covered_terms)

    payload = {
        "additionalContext": additional,
        "results": selected,
        "context_budget": {
            "schema_version": 3,
            "mode": mode,
            "max_bytes": budget["max_bytes"],
            "max_results": budget["max_results"],
            "ordinary_result_cap": max_ordinary,
            "requested_limit": requested_limit,
            "candidate_limit": scan_limit,
            "selected_results": len(selected),
            "available_results": len(results),
            "valid_results": len(valid_candidates),
            "considered_results": len(candidates),
            "candidate_cap_pruned": candidate_cap_pruned,
            "protected_signals": list(PROTECTED_SIGNALS),
            "protected_provenance_keys": list(PROTECTED_PROVENANCE_KEYS),
            "negative_evidence_signals": list(NEGATIVE_EVIDENCE_SIGNALS),
            "query_aware": bool(query_tokens),
            "query_terms": len(query_tokens),
            "query_terms_available": len(available_terms),
            "query_terms_covered": len(covered_terms),
            "coverage_ratio": round(len(covered_terms) / len(query_tokens), 6) if query_tokens else None,
            "input_coverage_ratio": (
                round(len(available_terms) / len(query_tokens), 6) if query_tokens else None
            ),
            "coverage_preservation_ratio": (
                round(len(covered_terms) / len(available_terms), 6)
                if available_terms
                else (1.0 if query_tokens else None)
            ),
            "query_terms_lost": lost_query_terms,
            "duplicate_similarity": duplicate_threshold,
            "redundancy_pruned": redundancy_pruned,
            "result_cap_pruned": result_cap_pruned,
            "byte_pruned": byte_pruned,
            "protected_available": protected_available,
            "protected_selected": protected_selected,
            "protected_dropped": max(0, protected_available - protected_selected),
            "negative_evidence_available": negative_available,
            "negative_evidence_selected": negative_selected,
            "negative_evidence_dropped": max(0, negative_available - negative_selected),
            "protected_provenance_available": protected_provenance_available,
            "protected_provenance_selected": protected_provenance_selected,
            "protected_provenance_dropped": max(
                0,
                protected_provenance_available - protected_provenance_selected,
            ),
            "provenance_available": provenance_available,
            "provenance_selected": provenance_selected,
            "input_bytes": input_bytes,
            "bytes": output_bytes,
            "saved_bytes": saved_bytes,
            "savings_ratio": round(saved_bytes / input_bytes, 6) if input_bytes else 0.0,
            "input_lexical_tokens": input_lexical_tokens,
            "output_lexical_tokens": output_lexical_tokens,
            "saved_lexical_tokens": saved_lexical_tokens,
            "lexical_savings_ratio": (
                round(saved_lexical_tokens / input_lexical_tokens, 6)
                if input_lexical_tokens
                else 0.0
            ),
            "truncated": (
                len(selected) < len(candidates)
                or candidate_cap_pruned > 0
                or redundancy_pruned > 0
                or result_cap_pruned > 0
                or byte_pruned > 0
            ),
            "over_budget_to_preserve": over_budget_to_preserve,
            "pre_byte_fit_results": len(before_byte_fit),
        },
    }
    context_metadata = payload["context_budget"]
    payload["retrieval_observation"] = build_retrieval_observation(
        operation="context.compress",
        query=query or "",
        started_ns=started_ns,
        returned=len(selected),
        candidates=len(candidates),
        partial=bool(context_metadata["truncated"]),
        policy=mode,
        sources={
            "input_results": len(results),
            "valid_results": len(valid_candidates),
            "considered_results": len(candidates),
        },
        limits={
            "max_bytes": budget["max_bytes"],
            "max_results": budget["max_results"],
            "requested_limit": requested_limit,
            "candidate_limit": scan_limit,
            "duplicate_similarity": duplicate_threshold,
        },
        quality={
            "coverage_ratio": context_metadata["coverage_ratio"],
            "coverage_preservation_ratio": context_metadata["coverage_preservation_ratio"],
            "redundancy_pruned": redundancy_pruned,
            "candidate_cap_pruned": candidate_cap_pruned,
            "byte_pruned": byte_pruned,
            "saved_bytes": saved_bytes,
            "savings_ratio": context_metadata["savings_ratio"],
            "saved_lexical_tokens": saved_lexical_tokens,
            "lexical_savings_ratio": context_metadata["lexical_savings_ratio"],
            "protected_dropped": context_metadata["protected_dropped"],
            "negative_evidence_dropped": context_metadata["negative_evidence_dropped"],
            "protected_provenance_dropped": context_metadata["protected_provenance_dropped"],
            "over_budget_to_preserve": over_budget_to_preserve,
        },
    )
    return payload
