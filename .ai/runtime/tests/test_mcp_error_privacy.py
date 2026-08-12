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


def test_schema_rejection_reason_reaches_the_client(tmp_path: Path) -> None:
    """The client must learn *why* the call was rejected, or it just retries."""
    response = mcp_server.handle_request(
        tmp_path,
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "code_query", "arguments": {"query": ""}},
        },
    )

    payload = response["result"]
    assert payload["isError"] is True
    assert payload["content"] == [
        {"type": "text", "text": "invalid tool arguments: arguments.query: must not be blank"}
    ]


def test_schema_rejection_never_echoes_an_undeclared_field_name(tmp_path: Path) -> None:
    """An unknown key is caller-chosen text and must not survive into the reply."""
    supplied = _sensitive_value()

    response = mcp_server.handle_request(
        tmp_path,
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "code_query",
                "arguments": {"query": "needle", supplied: "x" * 2_000_000},
            },
        },
    )

    serialized = json.dumps(response)
    payload = response["result"]
    assert payload["isError"] is True
    assert payload["content"] == [
        {"type": "text", "text": "invalid tool arguments: schema validation failed"}
    ]
    assert supplied not in serialized


def test_repeated_rejection_stop_order_reaches_the_client(tmp_path: Path) -> None:
    mcp_server.reset_repeated_rejections()
    request = {
        "jsonrpc": "2.0",
        "id": 8,
        "method": "tools/call",
        "params": {"name": "code_query", "arguments": {"query": "  "}},
    }

    for _attempt in range(mcp_server.MCP_REPEATED_REJECTION_LIMIT - 1):
        mcp_server.handle_request(tmp_path, dict(request))

    response = mcp_server.handle_request(tmp_path, dict(request))

    text = response["result"]["content"][0]["text"]
    assert "stop retrying this call" in text
    mcp_server.reset_repeated_rejections()


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (mcp_server.ToolArgumentError("invalid tool arguments: arguments.query: required"),
         "invalid tool arguments: arguments.query: required"),
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
