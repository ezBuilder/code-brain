"""Agent-driven query: deterministic retrieval + provenance/laundering guard (PRD §3.3/§12.2.6).

The runtime retrieves (FTS5 BM25) and attaches each candidate page's trust signals
(status, taint). Draft/quarantined pages are NOT silently fed back as trusted context —
they are split into `quarantined` so the calling agent cites them only with explicit
caution (injection-laundering defense). The agent writes the cited answer; file_back is
a separate ingest.commit_pages call.
"""
from __future__ import annotations

from pathlib import Path

from . import storage, fts as fts_mod
from . import lint as lint_mod


def query(ar_root: Path, question: str, k: int = 10) -> dict:
    """Return ranked candidates with per-page trust signals.

    candidates: trusted (status=active, not taint). quarantined: draft or taint pages,
    excluded from candidates. The agent must treat quarantined pages as low-trust.
    """
    hits = fts_mod.search(ar_root, question, k=k)
    if hits and isinstance(hits[0], dict) and "error" in hits[0]:
        return {"candidates": [], "quarantined": [], "error": hits[0]["error"]}
    wiki = storage.wiki_root(ar_root)
    wiki_resolved = wiki.resolve()
    trusted: list[dict] = []
    quarantined: list[dict] = []
    for h in hits:
        rel = h.get("page", "")
        page_path = wiki / rel
        # fail-closed: anything we cannot read as a wiki-internal file is quarantined,
        # never silently trusted (path traversal, missing file, read error).
        try:
            inside = page_path.resolve().is_relative_to(wiki_resolved)
        except (OSError, ValueError):
            inside = False
        if not inside or not page_path.is_file():
            quarantined.append({**h, "status": "unreadable", "taint": True})
            continue
        try:
            text = page_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            quarantined.append({**h, "status": "unreadable", "taint": True})
            continue
        meta = lint_mod.parse_frontmatter(text)
        status = meta.get("status", "active")
        taint = str(meta.get("taint", "")).lower() == "true"
        entry = {**h, "status": status, "taint": taint}
        (quarantined if (status == "draft" or taint) else trusted).append(entry)
    return {
        "candidates": trusted,
        "quarantined": quarantined,
        "note": (
            "quarantined pages (draft/taint) are excluded from candidates; "
            "cite them only with explicit low-trust caution"
            if quarantined else ""
        ),
    }
