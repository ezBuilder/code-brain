"""Opt-in compact tools mode (landscape P1) — default off, big token cut when on."""
from __future__ import annotations

from pathlib import Path

from ai_core import mcp_server as m


def test_default_is_full(monkeypatch) -> None:
    monkeypatch.delenv("AI_MCP_COMPACT_TOOLS", raising=False)
    m._invalidate_tools_list_cache()
    tools = m._build_tools_list_payload()["tools"]
    assert len(tools) > 60  # full catalog by default — no behavior change


def test_compact_returns_core_plus_search(monkeypatch) -> None:
    monkeypatch.setenv("AI_MCP_COMPACT_TOOLS", "1")
    m._invalidate_tools_list_cache()
    names = {t["name"] for t in m._build_tools_list_payload()["tools"]}
    assert "code_query" in names and "tool_search" in names
    assert "autoresearch_ingest_stage" not in names  # deferred
    assert len(names) < 20
    m._invalidate_tools_list_cache()


def test_tool_search_recovers_deferred(tmp_path: Path) -> None:
    r = m._dispatch_tool(tmp_path, "tool_search", {"query": "autoresearch ingest"})
    assert r["ok"] and any("ingest" in t["name"] for t in r["tools"])
