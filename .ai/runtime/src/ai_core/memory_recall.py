"""Unified read-time recall across durable memory (decisions + failures + lessons + procedures).

Inspired by memanto's recall/answer triad — but the network/LLM-synthesis half is intentionally
dropped. This module is pure-local, read-only, stdlib + existing scorers. It generalizes
lessons.recall_lessons' ``confidence * relevance * recency`` ranking to every durable store and
assembles one ranked citation block, so an agent can ask "what do I already know about X?"
mid-session and get folded failures, decisions, lessons, and procedures in a single answer.

No LLM, no network. The MCP boundary redacts the response; stored rows are already redacted at
write time, so this only reads and ranks.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .lessons import _parse_ts, recall_lessons
from .memory import read_decisions_filtered
from .procedural_memory import search_procedures

# Type priors: how much to trust each store before relevance/recency weighting.
# Explicit decisions are deliberate, so they outrank inferred/observed signals.
_DECISION_PRIOR = 0.9
_PROCEDURE_PRIOR = 0.7
_FAILURE_PRIOR = {"observed": 0.6, "confirmed": 0.85}  # stale/refuted never surface

_VALID_TYPES = frozenset({"decision", "failure", "lesson", "procedure"})


def _tokens(text: str) -> list[str]:
    return [t for t in str(text or "").strip().lower().split() if t]


def _relevance(query_tokens: list[str], haystack: str) -> float:
    if not query_tokens:
        return 0.0
    hay = haystack.lower()
    hits = sum(1 for t in query_tokens if t in hay)
    return hits / len(query_tokens)


def _recency(last: datetime | None, now: datetime) -> float:
    if last is None:
        days = 365.0
    else:
        days = max(0.0, (now - last).total_seconds() / 86400.0)
    return 1.0 / (1.0 + days * 0.01)


def recall_memory(
    root: Path,
    *,
    query: str,
    limit: int = 8,
    types: list[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Rank durable memory for a query across stores: confidence * relevance * recency.

    Args:
        root: project root
        query: free-text query (space-separated tokens)
        limit: max items returned
        types: optional subset of {decision, failure, lesson, procedure}; default = all
        now: clock override (tests)

    Returns {"ok": True, "count": n, "query": ..., "items": [...], "block": markdown}.
    Each item: {"kind", "ref", "text", "recall_score", "relevance", ...}. Read-only, fail-soft.
    """
    moment = now or datetime.now(timezone.utc)
    tokens = _tokens(query)
    wanted = {t for t in (types or []) if t in _VALID_TYPES} or set(_VALID_TYPES)
    if not tokens:
        return {"ok": True, "count": 0, "query": str(query or ""), "items": [], "block": ""}

    items: list[dict[str, Any]] = []

    # Lessons: reuse the existing confidence*relevance*recency scorer verbatim.
    if "lesson" in wanted:
        try:
            for it in recall_lessons(root, query=query, limit=limit * 3, now=moment).get("items", []):
                items.append({
                    "kind": "lesson",
                    "ref": str(it.get("id") or it.get("fingerprint") or "")[:80],
                    "text": str(it.get("fix") or it.get("failure") or "")[:300],
                    "recall_score": float(it.get("recall_score", 0.0)),
                    "relevance": float(it.get("relevance", 0.0)),
                    "confidence": it.get("confidence"),
                })
        except Exception:
            pass

    # Decisions + failures: prior(by kind/status) * token relevance * recency.
    want_dec = "decision" in wanted
    want_fail = "failure" in wanted
    if want_dec or want_fail:
        try:
            rows = read_decisions_filtered(root, limit=10_000).get("items", [])
        except Exception:
            rows = []
        for rec in rows:
            is_fail = rec.get("kind") == "failure"
            if is_fail and not want_fail:
                continue
            if not is_fail and not want_dec:
                continue
            text = str(rec.get("decision", ""))
            rel = _relevance(tokens, text + " " + " ".join(str(t) for t in (rec.get("tags") or [])))
            if rel <= 0.0:
                continue
            if is_fail:
                prior = _FAILURE_PRIOR.get(str(rec.get("status", "observed")).lower())
                if prior is None:
                    continue  # retired/unknown status → skip
            else:
                prior = _DECISION_PRIOR
            last = _parse_ts(str(rec.get("observed_at") or rec.get("decided_at") or ""))
            score = prior * rel * _recency(last, moment)
            items.append({
                "kind": "failure" if is_fail else "decision",
                "ref": str(rec.get("id") or "")[:80],
                "text": text[:300],
                "recall_score": round(score, 6),
                "relevance": round(rel, 4),
                "status": rec.get("status") if is_fail else None,
            })

    # Procedures: token relevance over procedure/trigger/tags * prior * recency.
    if "procedure" in wanted:
        try:
            procs = search_procedures(root, query=query, limit=limit * 3).get("items", [])
        except Exception:
            procs = []
        for rec in procs:
            hay = " ".join(str(rec.get(k, "")) for k in ("procedure", "trigger", "kind"))
            hay += " " + " ".join(str(t) for t in (rec.get("tags") or []))
            rel = _relevance(tokens, hay)
            if rel <= 0.0:
                continue
            last = _parse_ts(str(rec.get("ts") or ""))
            score = _PROCEDURE_PRIOR * rel * _recency(last, moment)
            items.append({
                "kind": "procedure",
                "ref": str(rec.get("id") or rec.get("trigger") or "")[:80],
                "text": str(rec.get("procedure", ""))[:300],
                "recall_score": round(score, 6),
                "relevance": round(rel, 4),
            })

    items.sort(key=lambda r: r["recall_score"], reverse=True)
    top = items[: max(0, int(limit))]
    return {
        "ok": True,
        "count": len(top),
        "query": str(query or ""),
        "items": top,
        "block": format_recall_block(query, top),
    }


_KIND_LABEL = {"decision": "결정", "failure": "실패(관측)", "lesson": "교훈", "procedure": "절차"}


def format_recall_block(query: str, items: list[dict[str, Any]]) -> str:
    """Render ranked recall items as a compact, citation-style markdown block (no LLM)."""
    if not items:
        return f"### Memory recall: {str(query)[:80]}\n(관련 메모리 없음)"
    lines = [f"### Memory recall: {str(query)[:80]}"]
    for it in items:
        label = _KIND_LABEL.get(str(it.get("kind")), str(it.get("kind")))
        ref = it.get("ref") or "?"
        score = it.get("recall_score", 0.0)
        text = str(it.get("text", "")).replace("\n", " ").strip()
        lines.append(f"- **[{label}]** ({ref}, score={score}) {text}")
    return "\n".join(lines)
