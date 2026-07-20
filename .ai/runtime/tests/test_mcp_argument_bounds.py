from __future__ import annotations

from pathlib import Path

import pytest

from ai_core import mcp_server


def _nested_mapping(depth: int):
    value: object = "leaf"
    for _index in range(depth):
        value = {"child": value}
    return value


def test_schema_specific_string_bound_rejects_before_handler(tmp_path: Path) -> None:
    marker = "SensitivePatternMarkerQ7"
    oversized = marker + "x" * 5000

    with pytest.raises(ValueError, match=r"arguments\.pattern: text too long") as exc_info:
        mcp_server._dispatch_tool(
            tmp_path,
            "ast_grep_search",
            {"pattern": oversized, "lang": "python"},
        )

    assert marker not in str(exc_info.value)
    assert oversized not in str(exc_info.value)


def test_schema_specific_array_bound_rejects_before_handler(tmp_path: Path) -> None:
    paths = [f"src/file-{index}.py" for index in range(101)]

    with pytest.raises(ValueError, match=r"arguments\.paths: too many items"):
        mcp_server._dispatch_tool(
            tmp_path,
            "code_graph_impact",
            {"paths": paths},
        )


def test_global_depth_bound_rejects_nested_unknown_value() -> None:
    error = mcp_server._validate_tool_arguments(
        "memory_query",
        {"query": "needle", "extra": _nested_mapping(mcp_server.MCP_ARGUMENT_MAX_DEPTH + 2)},
    )

    assert error is not None
    assert error.endswith("nesting too deep")


def test_global_node_bound_rejects_wide_nested_payload() -> None:
    payload = {
        "query": "needle",
        "extra": {
            f"key-{index}": list(range(10))
            for index in range(mcp_server.MCP_ARGUMENT_MAX_OBJECT_KEYS)
        },
    }

    error = mcp_server._validate_tool_arguments("memory_query", payload)

    assert error is not None
    assert error.endswith("too many values")


def test_global_total_character_bound_rejects_large_composite_payload() -> None:
    segment = "x" * 900_000
    payload = {
        "query": "needle",
        "extra": [segment, segment, segment, segment, segment],
    }

    error = mcp_server._validate_tool_arguments("memory_query", payload)

    assert error is not None
    assert error.endswith("total text too large")


def test_nonfinite_number_is_rejected() -> None:
    assert mcp_server._validate_tool_arguments(
        "memory_query",
        {"query": "needle", "limit": float("nan")},
    ) == "arguments.limit: non-finite number"
    assert mcp_server._validate_tool_arguments(
        "memory_query",
        {"query": "needle", "limit": float("inf")},
    ) == "arguments.limit: non-finite number"


def test_non_mapping_arguments_are_rejected() -> None:
    assert mcp_server._validate_tool_arguments(
        "memory_query",
        [],
    ) == "arguments: expected object"


def test_valid_payload_reaches_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_query(root, text, *, limit, evidence_source):
        captured.update(root=root, text=text, limit=limit, source=evidence_source)
        return {"ok": True, "results": []}

    monkeypatch.setattr(mcp_server, "query", fake_query)

    payload = mcp_server._dispatch_tool(
        tmp_path,
        "memory_query",
        {"query": "needle", "limit": 5},
    )

    assert payload == {"ok": True, "results": []}
    assert captured == {
        "root": tmp_path,
        "text": "needle",
        "limit": 5,
        "source": "search",
    }


def test_global_string_bound_applies_when_schema_has_no_max_length() -> None:
    error = mcp_server._validate_tool_arguments(
        "memory_query",
        {
            "query": "needle",
            "extra": "x" * (mcp_server.MCP_ARGUMENT_MAX_STRING_CHARS + 1),
        },
    )

    assert error == "arguments.extra: text too long"


def test_global_array_and_object_key_bounds_apply_to_unknown_fields() -> None:
    array_error = mcp_server._validate_tool_arguments(
        "memory_query",
        {
            "query": "needle",
            "extra": [0] * (mcp_server.MCP_ARGUMENT_MAX_ARRAY_ITEMS + 1),
        },
    )
    object_error = mcp_server._validate_tool_arguments(
        "memory_query",
        {
            "query": "needle",
            "extra": {
                f"key-{index}": index
                for index in range(mcp_server.MCP_ARGUMENT_MAX_OBJECT_KEYS + 1)
            },
        },
    )

    assert array_error == "arguments.extra: too many items"
    assert object_error == "arguments.extra: too many keys"


def test_unknown_tool_still_gets_global_shape_validation() -> None:
    error = mcp_server._validate_tool_arguments(
        "unknown_tool",
        {"payload": _nested_mapping(mcp_server.MCP_ARGUMENT_MAX_DEPTH + 2)},
    )

    assert error is not None
    assert error.endswith("nesting too deep")
