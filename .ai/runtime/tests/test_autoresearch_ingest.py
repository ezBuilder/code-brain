"""Stage 0 ingest pipeline + MCP dispatch end-to-end (agent-driven, deterministic)."""
from __future__ import annotations

import pathlib

import pytest

from ai_core.autoresearch import storage, ingest, fts


@pytest.fixture()
def ar(tmp_path: pathlib.Path) -> pathlib.Path:
    r = tmp_path / "ar"
    storage.ensure_tree(r)
    return r


def test_stage_idempotent_and_wrapped(ar):
    st = ingest.stage_source(ar, content="Reciprocal rank fusion combines BM25 and dense.", title="RRF")
    assert not st["duplicate"]
    assert st["source_id"].startswith("src_")
    assert "UNTRUSTED-DATA" in st["wrapped"] and st["nonce"] in st["wrapped"]
    st2 = ingest.stage_source(ar, content="Reciprocal rank fusion combines BM25 and dense.", title="RRF")
    assert st2["duplicate"] and st2["source_id"] == st["source_id"]


def test_commit_active_page_is_searchable(ar):
    st = ingest.stage_source(ar, content="Reciprocal rank fusion combines BM25 and dense retrieval.")
    res = ingest.commit_pages(ar, source_id=st["source_id"], pages=[{
        "rel_path": "concepts/rrf.md", "type": "concept", "title": "RRF",
        "content": "RRF combines BM25 and dense retrieval signals.",
        "sources": [st["source_id"]],
        "citations": [{"quote": "combines BM25 and dense", "sources": [st["source_id"]]}],
    }])
    assert res["written"] == ["concepts/rrf.md"] and res["drafted"] == []
    hits = fts.search(ar, "dense", k=5)
    assert hits and hits[0]["page"] == "concepts/rrf.md"
    # frontmatter persisted with active status
    page = (storage.wiki_root(ar) / "concepts" / "rrf.md").read_text(encoding="utf-8")
    assert "status: active" in page and st["source_id"] in page


def test_commit_quarantines_unverifiable_citation(ar):
    st = ingest.stage_source(ar, content="some genuine source text about hybrid search")
    res = ingest.commit_pages(ar, source_id=st["source_id"], pages=[{
        "rel_path": "concepts/bad.md", "content": "fabricated claim",
        "sources": [st["source_id"]],
        "citations": [{"quote": "THIS PHRASE IS NOT IN THE SOURCE", "sources": [st["source_id"]]}],
    }])
    assert res["drafted"] == ["concepts/bad.md"] and res["written"] == []
    page = (storage.wiki_root(ar) / "concepts" / "bad.md").read_text(encoding="utf-8")
    assert "status: draft" in page


def test_mcp_dispatch_end_to_end(tmp_path):
    from ai_core import mcp_server
    proj = tmp_path
    ar_root = storage.data_root(proj)
    storage.ensure_tree(ar_root)
    # stage + commit via dispatch
    st = mcp_server._dispatch_tool(proj, "autoresearch_ingest_stage",
                                   {"content": "hybrid search blends bm25 and embeddings"})
    sid = st["source_id"]
    mcp_server._dispatch_tool(proj, "autoresearch_ingest_commit", {
        "source_id": sid,
        "pages": [{"rel_path": "concepts/hybrid.md",
                   "content": "hybrid blends bm25 and embeddings",
                   "sources": [sid]}],
    })
    out = mcp_server._dispatch_tool(proj, "autoresearch_search", {"q": "embeddings", "k": 5})
    assert out["results"] and "hybrid" in out["results"][0]["page"]


def test_mcp_tools_registered():
    from ai_core import mcp_server
    assert "autoresearch_search" in mcp_server.TOOL_NAMES
    assert "autoresearch_ingest_stage" in mcp_server.TOOL_NAMES
    assert "autoresearch_ingest_commit" in mcp_server.TOOL_NAMES
