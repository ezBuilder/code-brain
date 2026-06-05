"""Deterministic verification gate (Stage 0). NO LLM — regex + substring + manifest lookup.

This is the cheap, deterministic subset of verification. The LLM-as-judge
faithfulness/factuality pipeline (5 stages) is deferred to Stage 3
`autoresearch_verify`, which breaks the Stage0→Stage3 circular dependency
(PRD §12.2.4). A hard-fail here forces the claim/page to status:draft.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import manifest as manifest_mod
from .models import VerifyDetResult

_ID_RE = re.compile(r"[A-Za-z0-9_\-]+")
_CITE_RE = re.compile(r"\[\[(?P<a>[A-Za-z0-9_\-]+)\]\]|\(source:\s*(?P<b>[A-Za-z0-9_\-]+)\)")


def parse_citations(text: str) -> list[str]:
    """Extract cited source ids from `[[id]]` or `(source: id)` markers."""
    ids: list[str] = []
    for m in _CITE_RE.finditer(text):
        ids.append(m.group("a") or m.group("b"))
    return ids


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def verify_claim(
    root: Path,
    source_ids: list[str],
    quote: str,
    source_texts: dict[str, str] | None = None,
) -> VerifyDetResult:
    """Deterministic checks for a single claim.

    (1) format: ≥1 source id, all well-formed.
    (2) substring: `quote` appears in at least one cited source text (normalized).
    (3) existence: every source id exists in manifest.jsonl.
    """
    source_texts = source_texts or {}
    reasons: list[str] = []

    format_ok = bool(source_ids) and all(_ID_RE.fullmatch(s or "") for s in source_ids)
    if not format_ok:
        reasons.append("citation_format")

    sources_exist = bool(source_ids) and all(manifest_mod.id_exists(root, s) for s in source_ids)
    if not sources_exist:
        reasons.append("source_id_missing")

    if quote.strip():
        nq = _normalize(quote)
        substring_ok = any(
            nq in _normalize(source_texts[sid])
            for sid in source_ids
            if sid in source_texts
        )
    else:
        substring_ok = True  # no direct quote to verify
    if not substring_ok:
        reasons.append("quote_not_in_source")

    return VerifyDetResult(
        format_ok=format_ok,
        substring_ok=substring_ok,
        sources_exist=sources_exist,
        failed_reasons=reasons,
    )
