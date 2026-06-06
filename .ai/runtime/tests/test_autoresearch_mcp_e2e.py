"""MCP protocol end-to-end for autoresearch_* (handle_request, not just _dispatch_tool).

Exercises the full JSON-RPC path a real MCP client uses: tools/list, tools/call wrapping,
unknown-tool error — across the stage→commit→query→lint→search workflow.
"""
from __future__ import annotations

from ai_core import mcp_server
from ai_core.autoresearch import storage


def _call(proj, name, args):
    resp = mcp_server.handle_request(proj, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": args},
    })
    assert resp is not None and "result" in resp, resp
    assert resp["result"]["isError"] is False
    return resp["result"]["structuredContent"]


def test_mcp_protocol_full_workflow(tmp_path):
    proj = tmp_path
    storage.ensure_tree(storage.data_root(proj))

    st = _call(proj, "autoresearch_ingest_stage",
               {"content": "reciprocal rank fusion combines bm25 and dense retrieval"})
    sid = st["source_id"]
    assert sid and st["nonce"]

    commit = _call(proj, "autoresearch_ingest_commit", {
        "source_id": sid,
        "pages": [{"rel_path": "concepts/rrf.md", "content": "rrf fuses bm25 and dense signals",
                   "sources": [sid], "citations": [{"quote": "bm25", "sources": [sid]}]}],
    })
    assert commit["written"] == ["concepts/rrf.md"]

    q = _call(proj, "autoresearch_query", {"question": "dense", "k": 5})
    assert any(c["page"] == "concepts/rrf.md" for c in q["candidates"])

    lint = _call(proj, "autoresearch_lint", {})
    assert lint["page_count"] == 1 and "concepts/rrf.md" in lint["orphans"]

    s = _call(proj, "autoresearch_search", {"q": "dense", "k": 5})
    assert s["results"] and s["results"][0]["page"] == "concepts/rrf.md"


def test_mcp_unknown_tool_errors(tmp_path):
    resp = mcp_server.handle_request(tmp_path, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "autoresearch_bogus", "arguments": {}}})
    assert "error" in resp and resp["error"]["code"] == -32602


def test_mcp_tools_list_includes_autoresearch(tmp_path):
    resp = mcp_server.handle_request(tmp_path, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    names = {t["name"] for t in resp["result"]["tools"]}
    assert {"autoresearch_search", "autoresearch_ingest_stage", "autoresearch_ingest_commit",
            "autoresearch_query", "autoresearch_lint"} <= names


def test_mcp_ingest_stage_rejects_empty_content(tmp_path):
    resp = mcp_server.handle_request(tmp_path, {
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "autoresearch_ingest_stage", "arguments": {"content": ""}}})
    # empty content → ValueError in dispatch → wrapped as result.isError (not JSON-RPC error)
    assert resp["result"]["isError"] is True
