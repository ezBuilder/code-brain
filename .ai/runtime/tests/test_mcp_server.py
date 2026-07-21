from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core import mcp_server  # noqa: E402
from ai_core.mcp_catalog_meta import MCP_METHOD_COUNT  # noqa: E402
from ai_core.policy import PolicyDenied  # noqa: E402


def test_lightweight_catalog_count_matches_server_tools() -> None:
    assert MCP_METHOD_COUNT == len(mcp_server.MCP_METHODS)


def test_tools_list_response_shape(tmp_path: Path) -> None:
    """tools/list returns a well-formed JSON-RPC response with the tool catalog."""
    mcp_server._invalidate_tools_list_cache()
    response = mcp_server.handle_request(tmp_path, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response is not None
    assert response["jsonrpc"] == "2.0"
    assert response["id"] == 1
    tools = response["result"]["tools"]
    assert isinstance(tools, list) and len(tools) > 0
    names = {t["name"] for t in tools}
    # Sanity: a few well-known tools should appear.
    assert "obs_usage" in names
    assert "memory_query" in names
    assert "code_read_hashline" in names
    assert "evidence_record" in names
    assert "evidence_list" in names
    assert "evidence_set_status" in names
    assert "security_finding_record" in names
    assert "security_finding_list" in names
    assert "security_finding_update" in names
    assert "stream_guard_scan" in names
    assert "ai_index_status" in names
    rebuild_tool = next(t for t in tools if t["name"] == "ai_request_rebuild")
    assert {"force", "incremental", "single_flight", "max_seconds"} <= set(
        rebuild_tool["inputSchema"]["properties"]
    )
    hashline_tool = next(t for t in tools if t["name"] == "code_read_hashline")
    assert "편집하기 전" in hashline_tool["description"]
    sandbox_tool = next(t for t in tools if t["name"] == "sandbox_execute")
    sandbox_properties = sandbox_tool["inputSchema"]["properties"]
    assert {"isolate_network", "isolate_env", "extra_env_vars"} <= set(sandbox_properties)


def test_sandbox_execute_forwards_isolation_options(tmp_path: Path, monkeypatch) -> None:
    captured: dict = {}

    def fake_execute(root: Path, **kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(mcp_server, "sandbox_execute", fake_execute)
    result = mcp_server._dispatch_tool(
        tmp_path,
        "sandbox_execute",
        {
            "command": ["echo", "ok"],
            "isolate_network": True,
            "isolate_env": True,
            "extra_env_vars": ["NODE_ENV"],
        },
    )
    assert result == {"ok": True}
    assert captured["isolate_network"] is True
    assert captured["isolate_env"] is True
    assert captured["extra_env_vars"] == ["NODE_ENV"]


def test_sandbox_execute_rejects_invalid_extra_env_name(tmp_path: Path) -> None:
    try:
        mcp_server._dispatch_tool(
            tmp_path,
            "sandbox_execute",
            {"command": ["echo", "ok"], "extra_env_vars": ["BAD-NAME"]},
        )
    except ValueError as exc:
        assert "invalid environment name" in str(exc)
    else:
        raise AssertionError("invalid environment name must fail closed")


def test_tools_list_response_cached(tmp_path: Path, monkeypatch) -> None:
    """tools/list payload is built once per process and reused across calls."""
    mcp_server._invalidate_tools_list_cache()
    call_count = {"n": 0}
    real_builder = mcp_server._build_tools_list_payload

    def counting_builder() -> dict:
        call_count["n"] += 1
        return real_builder()

    monkeypatch.setattr(mcp_server, "_build_tools_list_payload", counting_builder)
    # Re-trigger first build with the patched builder.
    mcp_server._invalidate_tools_list_cache()

    r1 = mcp_server.handle_request(tmp_path, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    r2 = mcp_server.handle_request(tmp_path, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    r3 = mcp_server.handle_request(tmp_path, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})

    assert call_count["n"] == 1, f"builder should run exactly once, ran {call_count['n']}"
    # request_id varies per call but the tools payload is identical.
    assert r1["id"] == 1 and r2["id"] == 2 and r3["id"] == 3
    assert r1["result"]["tools"] == r2["result"]["tools"] == r3["result"]["tools"]


def test_tools_list_cache_isolated_from_response_mutation(tmp_path: Path) -> None:
    """Mutating a returned response must not corrupt the cached payload."""
    mcp_server._invalidate_tools_list_cache()
    r1 = mcp_server.handle_request(tmp_path, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    # Mutate the returned response aggressively.
    r1["result"]["tools"].clear()
    r1["result"]["tools"].append({"name": "POISON"})

    r2 = mcp_server.handle_request(tmp_path, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in r2["result"]["tools"]}
    assert "POISON" not in names
    assert "obs_usage" in names


def test_invalidate_tools_list_cache_forces_rebuild(tmp_path: Path, monkeypatch) -> None:
    """_invalidate_tools_list_cache lets the next call rebuild the payload."""
    mcp_server._invalidate_tools_list_cache()
    call_count = {"n": 0}
    real_builder = mcp_server._build_tools_list_payload

    def counting_builder() -> dict:
        call_count["n"] += 1
        return real_builder()

    monkeypatch.setattr(mcp_server, "_build_tools_list_payload", counting_builder)
    mcp_server._invalidate_tools_list_cache()

    mcp_server.handle_request(tmp_path, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    mcp_server.handle_request(tmp_path, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert call_count["n"] == 1

    mcp_server._invalidate_tools_list_cache()
    mcp_server.handle_request(tmp_path, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    assert call_count["n"] == 2


def test_worker_pool_tools_hidden_from_default_but_callable(tmp_path: Path, monkeypatch) -> None:
    """C: worker-pool MCP tools are hidden from the default tools/list, yet still discoverable
    via tool_search and dispatchable directly (functionality intact)."""
    monkeypatch.delenv("AI_CODE_BRAIN_PROFILE", raising=False)
    monkeypatch.delenv("AI_MCP_COMPACT_TOOLS", raising=False)
    mcp_server._invalidate_tools_list_cache()
    resp = mcp_server.handle_request(tmp_path, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in resp["result"]["tools"]}
    assert "loopd_status" not in names and "loop_submit" not in names  # hidden
    assert "code_query" in names                                        # normal tools still shown
    # still dispatchable directly
    call = mcp_server.handle_request(tmp_path, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "loopd_status", "arguments": {}}})
    assert "result" in call and call["result"].get("structuredContent", {}).get("ok") is not None
    # still findable via tool_search
    ts = mcp_server.handle_request(tmp_path, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "tool_search", "arguments": {"query": "loopd worker pool"}}})
    found = {t["name"] for t in ts["result"]["structuredContent"]["tools"]}
    assert "loopd_status" in found
    mcp_server._invalidate_tools_list_cache()


def test_full_all_profile_resurfaces_hidden(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AI_CODE_BRAIN_PROFILE", "full-all")
    mcp_server._invalidate_tools_list_cache()
    resp = mcp_server.handle_request(tmp_path, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in resp["result"]["tools"]}
    assert "loopd_status" in names
    mcp_server._invalidate_tools_list_cache()


def test_index_status_and_rebuild_mcp_honor_policy(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / ".ai" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        "version: 1\nproject_name: mcp-index\nsearch:\n  retriever: bm25\n"
        "  indexing:\n    enabled: false\n    auto_rebuild: false\n",
        encoding="utf-8",
    )
    source = tmp_path / "src" / "main.py"
    source.parent.mkdir(parents=True)
    source.write_text("MCP_INDEX_NEEDLE = True\n", encoding="utf-8")

    status = mcp_server._dispatch_tool(tmp_path, "ai_index_status", {})
    denied = mcp_server._dispatch_tool(tmp_path, "ai_request_rebuild", {})
    forced = mcp_server._dispatch_tool(
        tmp_path,
        "ai_request_rebuild",
        {"force": True, "single_flight": False, "max_seconds": 30},
    )

    assert status["ok"] is True
    assert status["policy"]["enabled"] is False
    assert denied["ok"] is False
    assert denied["error"] == "INDEXING_DISABLED"
    assert forced["ok"] is True
    assert forced["committed"] is True

    monkeypatch.setenv("AI_CI", "1")
    with pytest.raises(PolicyDenied) as captured:
        mcp_server._dispatch_tool(tmp_path, "ai_request_rebuild", {"force": True})
    assert "CI" in str(captured.value).upper()
