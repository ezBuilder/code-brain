"""Query retrieval + laundering guard tests (PRD §3.3/§12.2.6)."""
from __future__ import annotations

from ai_core.autoresearch import query, ingest, storage, fts


def _seed(ar):
    st = ingest.stage_source(ar, content="dense retrieval and bm25 fusion source text")
    sid = st["source_id"]
    ingest.commit_pages(ar, source_id=sid, pages=[
        {"rel_path": "concepts/ok.md", "type": "concept", "title": "OK",
         "content": "dense bm25 fusion verified", "sources": [sid],
         "citations": [{"quote": "dense", "sources": [sid]}]},          # → active
        {"rel_path": "concepts/bad.md", "type": "concept", "title": "Bad",
         "content": "dense bm25 fabricated", "sources": [sid],
         "citations": [{"quote": "PHRASE NOT IN SOURCE", "sources": [sid]}]},  # → draft
    ])
    return sid


def test_query_returns_trusted_candidates(tmp_path):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    _seed(ar)
    res = query.query(ar, "dense", k=10)
    cand = [c["page"] for c in res["candidates"]]
    assert "concepts/ok.md" in cand
    assert all(c["status"] == "active" and not c["taint"] for c in res["candidates"])


def test_query_quarantines_draft(tmp_path):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    _seed(ar)
    res = query.query(ar, "dense", k=10)
    cand = [c["page"] for c in res["candidates"]]
    quar = [c["page"] for c in res["quarantined"]]
    assert "concepts/bad.md" in quar and "concepts/bad.md" not in cand
    assert res["note"]


def test_query_empty(tmp_path):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    res = query.query(ar, "nothing", k=5)
    assert res["candidates"] == [] and res["quarantined"] == []


def test_query_fail_closed_on_missing_file(tmp_path):
    # FTS-indexed but no wiki file on disk → must NOT be trusted (fail-closed)
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    fts.init_fts(ar)
    conn = fts.connect(ar)
    fts.upsert_page(conn, "ghost.md", "ghost.md", "sha", "dense bm25 phantom")
    conn.commit()
    conn.close()
    res = query.query(ar, "dense", k=5)
    assert all(c["page"] != "ghost.md" for c in res["candidates"])
    assert any(c["page"] == "ghost.md" for c in res["quarantined"])


def test_query_quarantines_path_traversal(tmp_path):
    ar = tmp_path / "ar"
    storage.ensure_tree(ar)
    fts.init_fts(ar)
    conn = fts.connect(ar)
    fts.upsert_page(conn, "../../etc/passwd", "../../etc/passwd", "sha", "dense bm25 secret")
    conn.commit()
    conn.close()
    res = query.query(ar, "dense", k=5)
    assert all("passwd" not in c["page"] for c in res["candidates"])


def test_query_mcp_dispatch(tmp_path):
    from ai_core import mcp_server
    proj = tmp_path
    ar = storage.data_root(proj)
    storage.ensure_tree(ar)
    _seed(ar)
    out = mcp_server._dispatch_tool(proj, "autoresearch_query", {"question": "dense", "k": 10})
    assert "concepts/ok.md" in [c["page"] for c in out["candidates"]]
    assert "concepts/bad.md" in [c["page"] for c in out["quarantined"]]
    assert "autoresearch_query" in mcp_server.TOOL_NAMES
