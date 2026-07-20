from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ai_core import mcp_server


def _sensitive_value() -> str:
    return "sk" + "_" + ("private-value-" * 8)


def test_unknown_tool_does_not_echo_supplied_name(tmp_path: Path) -> None:
    supplied = _sensitive_value()

    response = mcp_server.handle_request(
        tmp_path,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": supplied, "arguments": {}},
        },
    )

    serialized = json.dumps(response)
    assert response["error"] == {"code": -32602, "message": "unknown tool"}
    assert supplied not in serialized


def test_unknown_method_does_not_echo_supplied_method(tmp_path: Path) -> None:
    supplied = _sensitive_value()

    response = mcp_server.handle_request(
        tmp_path,
        {"jsonrpc": "2.0", "id": 2, "method": supplied},
    )

    serialized = json.dumps(response)
    assert response["error"] == {"code": -32601, "message": "method not found"}
    assert supplied not in serialized


def test_tool_handler_exception_does_not_echo_exception_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supplied = _sensitive_value()

    def fail(*_args, **_kwargs):
        raise ValueError(supplied)

    monkeypatch.setattr(mcp_server, "_dispatch_tool", fail)
    response = mcp_server.handle_request(
        tmp_path,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "code_query", "arguments": {"query": "x"}},
        },
    )

    serialized = json.dumps(response)
    payload = response["result"]
    assert payload["isError"] is True
    assert payload["content"] == [{"type": "text", "text": "invalid arguments"}]
    assert supplied not in serialized


def test_direct_dispatch_exception_is_generic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supplied = _sensitive_value()

    def fail(*_args, **_kwargs):
        raise RuntimeError(supplied)

    monkeypatch.setattr(mcp_server, "_dispatch_tool", fail)
    response = mcp_server.handle_request(
        tmp_path,
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "code_query",
            "params": {"query": "x"},
        },
    )

    serialized = json.dumps(response)
    assert response["error"] == {"code": -32000, "message": "operation failed"}
    assert supplied not in serialized


def test_invalid_resource_uri_does_not_echo_uri(tmp_path: Path) -> None:
    supplied = "codebrain://" + _sensitive_value()

    response = mcp_server.handle_request(
        tmp_path,
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "resources/read",
            "params": {"uri": supplied},
        },
    )

    serialized = json.dumps(response)
    assert response["error"] == {"code": -32602, "message": "invalid resource uri"}
    assert supplied not in serialized


def test_audit_normalizes_unknown_method_and_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict] = []
    supplied = _sensitive_value()
    monkeypatch.setattr(mcp_server, "is_ci", lambda: False)
    monkeypatch.setattr(mcp_server, "append_event", lambda _root, event: captured.append(event))

    mcp_server.record_mcp_request(
        tmp_path,
        supplied,
        {"method": supplied},
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "method not found"}},
        time.perf_counter(),
        None,
        tool_name=None,
    )

    assert captured[0]["method"] == "unknown"
    assert supplied not in json.dumps(captured)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (PermissionError("private"), "operation not permitted"),
        (ValueError("private"), "invalid arguments"),
        (TypeError("private"), "invalid arguments"),
        (KeyError("private"), "not found"),
        (TimeoutError("private"), "operation timed out"),
        (FileNotFoundError("private"), "required file not found"),
        (RuntimeError("private"), "operation failed"),
    ],
)
def test_safe_handler_error_is_bounded_and_value_independent(
    exc: BaseException,
    expected: str,
) -> None:
    assert mcp_server._safe_handler_error(exc) == expected
