"""Opt-in compact tools mode (landscape P1) — default off, big token cut when on."""
from __future__ import annotations

from pathlib import Path

from ai_core import mcp_server as m


def test_default_is_full(monkeypatch) -> None:
    monkeypatch.delenv("AI_MCP_COMPACT_TOOLS", raising=False)
    monkeypatch.delenv("AI_CODE_BRAIN_PROFILE", raising=False)
    m._invalidate_tools_list_cache()
    names = {t["name"] for t in m._build_tools_list_payload()["tools"]}
    # full catalog by default — every tool EXCEPT the hidden worker-pool surface, and far more
    # than the compact core. (full-all re-surfaces the hidden ones; see test_mcp_server.)
    assert len(names) == len(m.TOOLS) - len(m._HIDDEN_TOOLS) > len(m._CORE_TOOLS)
    assert not (names & m._HIDDEN_TOOLS)
    m._invalidate_tools_list_cache()


def test_compact_returns_core_plus_search(monkeypatch) -> None:
    monkeypatch.setenv("AI_MCP_COMPACT_TOOLS", "1")
    monkeypatch.delenv("AI_CODE_BRAIN_PROFILE", raising=False)
    m._invalidate_tools_list_cache()
    names = {t["name"] for t in m._build_tools_list_payload()["tools"]}
    assert "code_query" in names and "tool_search" in names
    assert "autoresearch_ingest_stage" not in names  # deferred
    assert len(names) <= 8
    m._invalidate_tools_list_cache()


def test_usage_profile_returns_five_hot_tools(monkeypatch) -> None:
    monkeypatch.setenv("AI_CODE_BRAIN_PROFILE", "usage")
    m._invalidate_tools_list_cache()
    names = {t["name"] for t in m._build_tools_list_payload()["tools"]}
    assert names == {"obs_usage", "code_query", "context_pack", "code_read_hashline", "tool_search"}
    m._invalidate_tools_list_cache()


def test_tool_search_recovers_deferred(tmp_path: Path) -> None:
    r = m._dispatch_tool(tmp_path, "tool_search", {"query": "autoresearch ingest"})
    assert r["ok"] and any("ingest" in t["name"] for t in r["tools"])
