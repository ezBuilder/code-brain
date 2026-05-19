from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core import mcp_server  # noqa: E402


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
