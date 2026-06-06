"""Deterministic wiki health lint (PRD §3.3). NO LLM, NO auto-fix — reports only.

Surfaces: orphan pages (no inbound links), draft/quarantined pages (verify-det failed),
taint warnings (laundering — page derived from a draft/quarantined source), and stale
pages (old `updated`). The calling agent decides remediation; lint never mutates the wiki.
stdlib only.
"""
from __future__ import annotations

from pathlib import Path

from . import storage


def parse_frontmatter(text: str) -> dict:
    """Minimal frontmatter reader matching ingest._frontmatter's writer (key: value)."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    meta: dict = {}
    for line in text[3:end].splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            k = key.strip()
            if k not in meta:  # first-wins: blocks duplicate-key status laundering (draft→active)
                meta[k] = val.strip()
    return meta


def _iter_pages(ar_root: Path):
    wiki = storage.wiki_root(ar_root)
    if not wiki.is_dir():
        return
    for md in sorted(wiki.rglob("*.md")):
        if md.name == storage.LOG_NAME:  # wiki/log.md is the append-only chronicle, not a page
            continue
        rel = str(md.relative_to(wiki))
        yield rel, parse_frontmatter(md.read_text(encoding="utf-8", errors="replace"))


def lint(ar_root: Path, *, stale_before: str | None = None) -> dict:
    """Return a deterministic health report. stale_before: ISO date string; pages whose
    `updated` is lexically < it are flagged stale (ISO dates sort lexically)."""
    pages = dict(_iter_pages(ar_root))
    inbound = {rel: 0 for rel in pages}
    for rel, meta in pages.items():
        links_raw = meta.get("links", "")
        for target in pages:
            if target != rel and target in links_raw:
                inbound[target] += 1
    orphans = [rel for rel, n in inbound.items() if n == 0]
    drafts = [rel for rel, m in pages.items() if m.get("status") == "draft"]
    taint = [rel for rel, m in pages.items() if str(m.get("taint", "")).lower() == "true"]
    stale = []
    if stale_before:
        stale = [rel for rel, m in pages.items()
                 if m.get("updated") and m["updated"] < stale_before]
    return {
        "page_count": len(pages),
        "orphans": sorted(orphans),
        "drafts": sorted(drafts),
        "taint_warnings": sorted(taint),
        "stale": sorted(stale),
    }
