"""Bounded local recall across durable decisions, failures, lessons and procedures."""
from __future__ import annotations

import heapq
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .lessons import _parse_ts, lesson_fingerprint, lessons_path
from .memory import decisions_path, read_jsonl_recent_bounded
from .memory_match import (
    flatten_evidence,
    stable_text_key,
    token_similarity,
    tokenize,
    weighted_relevance,
)
from .procedural_memory import procedural_path

_DECISION_PRIOR = 0.9
_PROCEDURE_PRIOR = 0.7
_FAILURE_PRIOR = {"observed": 0.6, "confirmed": 0.85}
_VALID_TYPES = frozenset({"decision", "failure", "lesson", "procedure"})
_RETIRED_FAILURE_STATUSES = frozenset({"stale", "refuted"})


def _bounded_env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _bounded_env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if value != value:
        value = default
    return max(minimum, min(maximum, value))


def policy() -> dict[str, int | float]:
    return {
        "max_records_per_store": _bounded_env_int(
            "AI_MEMORY_RECALL_MAX_RECORDS", 10_000, minimum=10, maximum=100_000
        ),
        "max_bytes_per_store": _bounded_env_int(
            "AI_MEMORY_RECALL_MAX_BYTES", 8_000_000, minimum=16_384, maximum=100_000_000
        ),
        "max_candidates": _bounded_env_int(
            "AI_MEMORY_RECALL_MAX_CANDIDATES", 1_000, minimum=16, maximum=20_000
        ),
        "duplicate_similarity": _bounded_env_float(
            "AI_MEMORY_RECALL_DUPLICATE_SIMILARITY", 0.86, minimum=0.5, maximum=1.0
        ),
    }


def _recency(last: datetime | None, now: datetime) -> float:
    days = 365.0 if last is None else max(0.0, (now - last).total_seconds() / 86400.0)
    return 1.0 / (1.0 + days * 0.01)


def _is_expired(value: object, now: datetime) -> bool:
    parsed = _parse_ts(str(value or ""))
    return parsed is not None and parsed < now


def _safe_versions(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key)[:40]: str(child)[:80]
        for key, child in list(value.items())[:8]
        if str(key).strip()
    }


def _relations_for(
    record: dict[str, Any],
    *,
    contradicted_by: dict[str, list[str]],
    derived_by: dict[str, list[str]],
) -> dict[str, Any]:
    ref = str(record.get("id") or "")
    relations: dict[str, Any] = {}
    for key in ("contradicts", "derives_from"):
        value = str(record.get(key) or "").strip()
        if value:
            relations[key] = value[:80]
    if ref in contradicted_by:
        relations["contradicted_by"] = contradicted_by[ref][:8]
    if ref in derived_by:
        relations["derived_by"] = derived_by[ref][:8]
    return relations


def _temporal_for(record: dict[str, Any], now: datetime) -> dict[str, Any]:
    valid_from = str(record.get("observed_at") or record.get("decided_at") or "").strip()
    expires_at = str(record.get("expires_at") or "").strip()
    retest_after = str(record.get("retest_after") or "").strip()
    result: dict[str, Any] = {"valid_from": valid_from[:40] if valid_from else None}
    if expires_at:
        result["expires_at"] = expires_at[:40]
    if retest_after:
        parsed = _parse_ts(retest_after)
        result["retest_after"] = retest_after[:40]
        result["retest_due"] = bool(parsed is not None and parsed <= now)
    return result


def _push_candidate(
    heap: list[tuple[float, int, dict[str, Any], set[str], str]],
    *,
    item: dict[str, Any],
    terms: set[str],
    text_key: str,
    sequence: int,
    maximum: int,
) -> bool:
    entry = (float(item["recall_score"]), sequence, item, terms, text_key)
    if len(heap) < maximum:
        heapq.heappush(heap, entry)
        return False
    if entry[:2] > heap[0][:2]:
        heapq.heapreplace(heap, entry)
    return True


def _scan_store(root: Path, path: Path, policy: dict[str, int | float]) -> dict[str, Any]:
    return read_jsonl_recent_bounded(
        path,
        max_records=int(policy["max_records_per_store"]),
        max_bytes=int(policy["max_bytes_per_store"]),
    )


def recall_memory(
    root: Path,
    *,
    query: str,
    limit: int = 8,
    types: list[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Recall durable memory with bounded scans and explainable local ranking."""
    from .retrieval_observation import build as build_retrieval_observation
    from .retrieval_observation import start as start_retrieval_observation

    started_ns = start_retrieval_observation()
    moment = now or datetime.now(timezone.utc)
    query_text = str(query or "")[:2048]
    query_tokens = tokenize(query_text)
    wanted = {str(item) for item in (types or []) if str(item) in _VALID_TYPES} or set(_VALID_TYPES)
    output_limit = max(0, int(limit))
    effective_policy = policy()
    base_scan: dict[str, Any] = {
        "complete": True,
        "partial": False,
        "stores": {},
        "candidates_considered": 0,
        "candidate_limit_dropped": 0,
        "duplicates_suppressed": 0,
        "expired_filtered": 0,
        "retired_filtered": 0,
        "policy": effective_policy,
    }
    if not query_tokens or output_limit == 0:
        payload = {
            "ok": True,
            "count": 0,
            "query": query_text,
            "items": [],
            "block": "" if not query_tokens else format_recall_block(query_text, []),
            "scan": base_scan,
        }
        payload["retrieval_observation"] = build_retrieval_observation(
            operation="memory.recall",
            query=query_text,
            started_ns=started_ns,
            returned=0,
            candidates=0,
            policy="bounded-jsonl-tail",
            limits=effective_policy,
            quality={"empty_query": not bool(query_tokens)},
        )
        return payload

    candidates: list[tuple[float, int, dict[str, Any], set[str], str]] = []
    sequence = 0

    def add(item: dict[str, Any], full_text: str) -> None:
        nonlocal sequence
        sequence += 1
        text_key, terms = stable_text_key(full_text)
        base_scan["candidates_considered"] += 1
        dropped = _push_candidate(
            candidates,
            item=item,
            terms=terms,
            text_key=text_key,
            sequence=sequence,
            maximum=int(effective_policy["max_candidates"]),
        )
        if dropped:
            base_scan["candidate_limit_dropped"] += 1

    if "decision" in wanted or "failure" in wanted:
        scanned = _scan_store(root, decisions_path(root), effective_policy)
        base_scan["stores"]["decisions"] = scanned.get("scan", {})
        rows = [item for item in scanned.get("items", []) if isinstance(item, dict)]
        plain: list[dict[str, Any]] = []
        failures: dict[str, dict[str, Any]] = {}
        for record in rows:
            if record.get("kind") == "failure":
                ref = str(record.get("id") or "")
                if ref:
                    failures[ref] = record
            else:
                plain.append(record)
        live_records: list[dict[str, Any]] = []
        for record in [*plain, *failures.values()]:
            if _is_expired(record.get("expires_at"), moment):
                base_scan["expired_filtered"] += 1
                continue
            if record.get("kind") == "failure" and str(record.get("status") or "observed").lower() in _RETIRED_FAILURE_STATUSES:
                base_scan["retired_filtered"] += 1
                continue
            live_records.append(record)

        contradicted_by: dict[str, list[str]] = {}
        derived_by: dict[str, list[str]] = {}
        for record in live_records:
            source_ref = str(record.get("id") or "")[:80]
            contradicts = str(record.get("contradicts") or "")
            derives_from = str(record.get("derives_from") or "")
            if source_ref and contradicts:
                contradicted_by.setdefault(contradicts, []).append(source_ref)
            if source_ref and derives_from:
                derived_by.setdefault(derives_from, []).append(source_ref)

        for record in live_records:
            is_failure = record.get("kind") == "failure"
            kind = "failure" if is_failure else "decision"
            if kind not in wanted:
                continue
            text = str(record.get("decision") or "")
            fields = {
                "text": (text, 1.0),
                "tags": (" ".join(str(tag) for tag in (record.get("tags") or [])), 1.15),
                "source": (record.get("source"), 0.65),
                "environment": (record.get("environment"), 0.8),
                "versions": (flatten_evidence(record.get("observed_versions")), 0.85),
            }
            relevance, match_fields = weighted_relevance(query_tokens, fields, query_text=query_text)
            if relevance <= 0.0:
                continue
            if is_failure:
                prior = _FAILURE_PRIOR.get(str(record.get("status") or "observed").lower())
                if prior is None:
                    continue
            else:
                prior = _DECISION_PRIOR
            last = _parse_ts(str(record.get("observed_at") or record.get("decided_at") or ""))
            score = prior * relevance * _recency(last, moment)
            item = {
                "kind": kind,
                "ref": str(record.get("id") or "")[:80],
                "text": text[:300],
                "recall_score": round(score, 6),
                "relevance": round(relevance, 4),
                "match_fields": match_fields,
                "provenance": {
                    "store": ".ai/memory/decisions.jsonl",
                    "source": str(record.get("source") or "operator")[:128],
                    "observed_versions": _safe_versions(record.get("observed_versions")),
                    "environment": str(record.get("environment") or "")[:160] or None,
                },
                "temporal": _temporal_for(record, moment),
                "relations": _relations_for(
                    record,
                    contradicted_by=contradicted_by,
                    derived_by=derived_by,
                ),
            }
            if is_failure:
                item["status"] = record.get("status")
            add(item, text)

    if "lesson" in wanted:
        scanned = _scan_store(root, lessons_path(root), effective_policy)
        base_scan["stores"]["lessons"] = scanned.get("scan", {})
        groups: dict[str, list[dict[str, Any]]] = {}
        order: list[str] = []
        for record in scanned.get("items", []):
            if not isinstance(record, dict):
                continue
            fingerprint = lesson_fingerprint(record)
            if fingerprint not in groups:
                groups[fingerprint] = []
                order.append(fingerprint)
            groups[fingerprint].append(record)
        for fingerprint in order:
            group = groups[fingerprint]
            latest = group[-1]
            confidence = 0.5
            for _ in range(len(group) - 1):
                confidence = min(1.0, confidence + 0.1 * (1.0 - confidence))
            last = max(
                (_parse_ts(str(record.get("created_at") or record.get("ts") or "")) for record in group),
                default=None,
                key=lambda value: value or datetime.min.replace(tzinfo=timezone.utc),
            )
            weeks = max(0.0, (moment - last).total_seconds() / (7 * 86400.0)) if last else 0.0
            confidence = max(0.05, confidence - 0.05 * weeks)
            if confidence <= 0.1 and len(group) <= 1:
                continue
            fields = {
                "failure": (latest.get("failure"), 1.0),
                "cause": (latest.get("cause"), 1.0),
                "fix": (latest.get("fix"), 1.1),
                "command": (latest.get("command"), 1.0),
                "outcome": (latest.get("outcome"), 0.8),
                "details": (latest.get("details"), 0.7),
                "tags": (" ".join(str(tag) for tag in (latest.get("tags") or [])), 1.15),
                "source": (latest.get("source"), 0.65),
            }
            relevance, match_fields = weighted_relevance(query_tokens, fields, query_text=query_text)
            if relevance <= 0.0:
                continue
            score = confidence * relevance * _recency(last, moment)
            text = str(latest.get("fix") or latest.get("failure") or latest.get("command") or "")
            item = {
                "kind": "lesson",
                "ref": str(latest.get("id") or fingerprint)[:80],
                "text": text[:300],
                "recall_score": round(score, 6),
                "relevance": round(relevance, 4),
                "confidence": round(confidence, 4),
                "match_fields": match_fields,
                "provenance": {
                    "store": ".ai/memory/lessons.jsonl",
                    "source": str(latest.get("source") or "operator")[:128],
                    "fingerprint": fingerprint,
                    "reinforcements": len(group),
                },
                "temporal": {"valid_from": str(latest.get("created_at") or latest.get("ts") or "")[:40] or None},
                "relations": {},
            }
            add(item, text)

    if "procedure" in wanted:
        scanned = _scan_store(root, procedural_path(root), effective_policy)
        base_scan["stores"]["procedures"] = scanned.get("scan", {})
        for record in scanned.get("items", []):
            if not isinstance(record, dict):
                continue
            evidence_text = flatten_evidence(record.get("evidence"))
            fields = {
                "procedure": (record.get("procedure"), 1.0),
                "trigger": (record.get("trigger"), 1.15),
                "kind": (record.get("kind"), 0.65),
                "tags": (" ".join(str(tag) for tag in (record.get("tags") or [])), 1.1),
                "evidence": (evidence_text, 0.75),
            }
            relevance, match_fields = weighted_relevance(query_tokens, fields, query_text=query_text)
            if relevance <= 0.0:
                continue
            last = _parse_ts(str(record.get("ts") or ""))
            score = _PROCEDURE_PRIOR * relevance * _recency(last, moment)
            item = {
                "kind": "procedure",
                "ref": str(record.get("id") or record.get("trigger") or "")[:80],
                "text": str(record.get("procedure") or "")[:300],
                "recall_score": round(score, 6),
                "relevance": round(relevance, 4),
                "match_fields": match_fields,
                "provenance": {
                    "store": ".ai/memory/procedural.jsonl",
                    "trigger": str(record.get("trigger") or "")[:128],
                    "evidence": evidence_text[:320] or None,
                },
                "temporal": {"valid_from": str(record.get("ts") or "")[:40] or None},
                "relations": {},
            }
            add(item, str(record.get("procedure") or ""))

    ranked = sorted(candidates, key=lambda entry: (-entry[0], -entry[1], str(entry[2].get("kind")), str(entry[2].get("ref"))))
    selected: list[dict[str, Any]] = []
    selected_terms: list[tuple[str, set[str]]] = []
    duplicate_threshold = float(effective_policy["duplicate_similarity"])
    for _score, _sequence, item, terms, text_key in ranked:
        duplicate = False
        for existing_key, existing_terms in selected_terms:
            if text_key and text_key == existing_key:
                duplicate = True
                break
            if min(len(terms), len(existing_terms)) >= 4 and token_similarity(terms, existing_terms) >= duplicate_threshold:
                duplicate = True
                break
        if duplicate:
            base_scan["duplicates_suppressed"] += 1
            continue
        selected.append(item)
        selected_terms.append((text_key, terms))
        if len(selected) >= output_limit:
            break

    store_scans = list(base_scan["stores"].values())
    base_scan["complete"] = all(bool(scan.get("complete", False)) for scan in store_scans) if store_scans else True
    base_scan["partial"] = not base_scan["complete"]
    payload = {
        "ok": True,
        "count": len(selected),
        "query": query_text,
        "items": selected,
        "block": format_recall_block(query_text, selected),
        "scan": base_scan,
    }
    payload["retrieval_observation"] = build_retrieval_observation(
        operation="memory.recall",
        query=query_text,
        started_ns=started_ns,
        returned=len(selected),
        candidates=int(base_scan["candidates_considered"]),
        partial=bool(base_scan["partial"]),
        policy="bounded-jsonl-tail",
        sources={
            name: {
                "records": int(scan.get("records_returned", 0) or 0),
                "partial": bool(scan.get("partial")),
            }
            for name, scan in base_scan["stores"].items()
            if isinstance(scan, dict)
        },
        limits=effective_policy,
        quality={
            "duplicates_suppressed": base_scan["duplicates_suppressed"],
            "candidate_limit_dropped": base_scan["candidate_limit_dropped"],
            "expired_filtered": base_scan["expired_filtered"],
            "retired_filtered": base_scan["retired_filtered"],
        },
    )
    return payload


_KIND_LABEL = {"decision": "결정", "failure": "실패(관측)", "lesson": "교훈", "procedure": "절차"}


def format_recall_block(query: str, items: list[dict[str, Any]]) -> str:
    """Render ranked recall items as a compact, citation-style markdown block."""
    if not items:
        return f"### Memory recall: {str(query)[:80]}\n(관련 메모리 없음)"
    lines = [f"### Memory recall: {str(query)[:80]}"]
    for item in items:
        label = _KIND_LABEL.get(str(item.get("kind")), str(item.get("kind")))
        ref = item.get("ref") or "?"
        score = item.get("recall_score", 0.0)
        text = str(item.get("text", "")).replace("\n", " ").strip()
        relation_count = sum(
            len(value) if isinstance(value, list) else 1
            for value in (item.get("relations") or {}).values()
            if value
        )
        relation_suffix = f", relations={relation_count}" if relation_count else ""
        lines.append(f"- **[{label}]** ({ref}, score={score}{relation_suffix}) {text}")
    return "\n".join(lines)


__all__ = ["format_recall_block", "policy", "recall_memory"]
