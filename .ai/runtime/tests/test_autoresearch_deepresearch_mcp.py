"""deepresearch MCP dispatch tests (Stage 3) — start/update/status round-trip."""
from __future__ import annotations

from ai_core import mcp_server
from ai_core.autoresearch import storage


def test_deepresearch_dispatch_roundtrip(tmp_path):
    proj = tmp_path
    storage.ensure_tree(storage.data_root(proj))
    s = mcp_server._dispatch_tool(proj, "autoresearch_deepresearch_start",
                                  {"question": "what is reciprocal rank fusion?"})
    sid = s["session_id"]
    assert sid.startswith("dr_") and s["status"] == "planning"
    mcp_server._dispatch_tool(proj, "autoresearch_deepresearch_update",
                              {"session_id": sid, "subquestions": ["a", "b"], "status": "collecting"})
    mcp_server._dispatch_tool(proj, "autoresearch_deepresearch_update",
                              {"session_id": sid, "add_source": "src_0123456789ab"})
    st = mcp_server._dispatch_tool(proj, "autoresearch_deepresearch_status", {"session_id": sid})
    assert st["status"] == "collecting" and st["subquestions"] == ["a", "b"]
    assert st["sources"] == ["src_0123456789ab"]


def test_deepresearch_status_missing(tmp_path):
    proj = tmp_path
    storage.ensure_tree(storage.data_root(proj))
    out = mcp_server._dispatch_tool(proj, "autoresearch_deepresearch_status",
                                    {"session_id": "dr_ffffffffffff"})
    assert out.get("error") == "session_not_found"


def test_deepresearch_tools_registered():
    for m in ("autoresearch_deepresearch_start", "autoresearch_deepresearch_update",
              "autoresearch_deepresearch_status"):
        assert m in mcp_server.TOOL_NAMES
