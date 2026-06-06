"""Wiki health lint tests (PRD §3.3) — deterministic, no auto-fix."""
from __future__ import annotations

from ai_core.autoresearch import lint, ingest, storage


def test_parse_frontmatter():
    fm = lint.parse_frontmatter('---\nid: x\nstatus: draft\ntaint: true\n---\n\nbody')
    assert fm["status"] == "draft" and fm["taint"] == "true" and fm["id"] == "x"


def test_parse_frontmatter_none_when_absent():
    assert lint.parse_frontmatter("no frontmatter here") == {}


def test_parse_frontmatter_first_wins():
    # duplicate-key laundering: a forged second `status: active` must NOT win
    fm = lint.parse_frontmatter("---\nstatus: draft\nstatus: active\n---\n\nbody")
    assert fm["status"] == "draft"


def test_lint_flags_orphans_and_drafts(tmp_path):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    st = ingest.stage_source(ar, content="reciprocal rank fusion bm25 dense source text")
    sid = st["source_id"]
    # one active page (good citation) + one draft page (unverifiable citation)
    ingest.commit_pages(ar, source_id=sid, pages=[
        {"rel_path": "concepts/good.md", "type": "concept", "title": "Good",
         "content": "rank fusion bm25", "sources": [sid],
         "citations": [{"quote": "rank fusion bm25", "sources": [sid]}]},
        {"rel_path": "concepts/bad.md", "type": "concept", "title": "Bad",
         "content": "fabricated", "sources": [sid],
         "citations": [{"quote": "PHRASE NOT IN SOURCE", "sources": [sid]}]},
    ])
    rep = lint.lint(ar)
    assert rep["page_count"] == 2
    assert "concepts/bad.md" in rep["drafts"]
    assert "concepts/good.md" not in rep["drafts"]
    # neither links the other → both orphan
    assert "concepts/good.md" in rep["orphans"] and "concepts/bad.md" in rep["orphans"]
    assert rep["taint_warnings"] == []


def test_lint_empty_corpus(tmp_path):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    rep = lint.lint(ar)
    assert rep == {"page_count": 0, "orphans": [], "drafts": [], "taint_warnings": [], "stale": []}


def test_lint_via_mcp_dispatch(tmp_path):
    from ai_core import mcp_server
    proj = tmp_path
    ar = storage.data_root(proj)
    storage.ensure_tree(ar)
    st = ingest.stage_source(ar, content="hybrid search source")
    ingest.commit_pages(ar, source_id=st["source_id"], pages=[
        {"rel_path": "concepts/h.md", "content": "hybrid search", "sources": [st["source_id"]]},
    ])
    rep = mcp_server._dispatch_tool(proj, "autoresearch_lint", {})
    assert rep["page_count"] == 1 and "concepts/h.md" in rep["orphans"]
    assert "autoresearch_lint" in mcp_server.TOOL_NAMES
