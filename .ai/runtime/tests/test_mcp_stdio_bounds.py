from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from ai_core import mcp_server


def _run_stdio(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    payload: str,
) -> list[dict]:
    stdin = io.StringIO(payload)
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    assert mcp_server.serve_stdio(root) == 0
    return [json.loads(line) for line in stdout.getvalue().splitlines() if line]


def test_non_object_request_is_rejected_without_stopping_stdio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = '[1, 2, 3]\n{"jsonrpc":"2.0","id":2,"method":"ping"}\n'

    responses = _run_stdio(monkeypatch, tmp_path, payload)

    assert responses[0]["error"]["code"] == -32600
    assert responses[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}


def test_invalid_params_shape_returns_invalid_params(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":[]}\n'

    response = _run_stdio(monkeypatch, tmp_path, payload)[0]

    assert response["id"] == 1
    assert response["error"] == {"code": -32602, "message": "params must be an object"}


def test_oversized_request_line_is_drained_and_next_request_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_server, "MCP_STDIN_MAX_BYTES", 64)
    oversized = '{"jsonrpc":"2.0","id":1,"method":"ping","pad":"' + ("x" * 100) + '"}\n'
    payload = oversized + '{"jsonrpc":"2.0","id":2,"method":"ping"}\n'

    responses = _run_stdio(monkeypatch, tmp_path, payload)

    assert responses[0]["error"] == {"code": -32600, "message": "request too large"}
    assert responses[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}


def test_parse_error_does_not_echo_parser_or_input_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _run_stdio(monkeypatch, tmp_path, "{secret-token\n")[0]

    serialized = json.dumps(response)
    assert response["error"] == {"code": -32700, "message": "parse error"}
    assert "secret-token" not in serialized
    assert "column" not in serialized


def test_invalid_envelope_fields_are_rejected() -> None:
    assert mcp_server.handle_request(Path("."), {"jsonrpc": "1.0", "id": 1, "method": "ping"}) == {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32600, "message": "invalid request"},
    }
    assert mcp_server.handle_request(Path("."), {"jsonrpc": "2.0", "id": True, "method": "ping"}) == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32600, "message": "invalid request id"},
    }


def test_method_and_request_id_lengths_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server, "MCP_METHOD_MAX_CHARS", 8)
    monkeypatch.setattr(mcp_server, "MCP_REQUEST_ID_MAX_CHARS", 4)

    method_response = mcp_server.handle_request(
        Path("."),
        {"jsonrpc": "2.0", "id": 1, "method": "x" * 9},
    )
    id_response = mcp_server.handle_request(
        Path("."),
        {"jsonrpc": "2.0", "id": "abcde", "method": "ping"},
    )

    assert method_response["error"]["code"] == -32600
    assert id_response["id"] is None
    assert id_response["error"]["message"] == "invalid request id"


def test_oversized_response_is_replaced_with_bounded_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_server, "MCP_RESPONSE_MAX_BYTES", 128)
    monkeypatch.setattr(
        mcp_server,
        "handle_request",
        lambda _root, request: {
            "jsonrpc": "2.0",
            "id": request.get("id"),
            "result": {"value": "x" * 1000},
        },
    )

    response = _run_stdio(
        monkeypatch,
        tmp_path,
        '{"jsonrpc":"2.0","id":7,"method":"ping"}\n',
    )[0]

    assert response == {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {"code": -32001, "message": "response too large"},
    }


def test_notification_still_produces_no_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _run_stdio(
        monkeypatch,
        tmp_path,
        '{"jsonrpc":"2.0","method":"notifications/initialized"}\n',
    )

    assert responses == []
