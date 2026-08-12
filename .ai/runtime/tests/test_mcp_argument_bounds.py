from __future__ import annotations

from pathlib import Path

import pytest

from ai_core import mcp_server


def _nested_mapping(depth: int):
    value: object = "leaf"
    for _index in range(depth):
        value = {"child": value}
    return value


@pytest.fixture(autouse=True)
def _reset_loop_guard() -> None:
    mcp_server.reset_repeated_rejections()


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


@pytest.mark.parametrize("tool_name", ["memory_query", "code_query", "context_pack", "obs_search"])
@pytest.mark.parametrize("blank", ["", " ", "\n\t "])
def test_blank_required_query_is_rejected_before_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    blank: str,
) -> None:
    """An empty query must fail loudly, not return a soft ``reason: empty_query`` body.

    A soft body is indistinguishable from a normal empty result to a naive client, which
    is exactly how a model ends up calling the same broken search a dozen times in a row.
    """

    def unreachable(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("handler must not run for a blank required query")

    monkeypatch.setattr(mcp_server, "query", unreachable)
    monkeypatch.setattr(mcp_server, "context_pack", unreachable)

    with pytest.raises(ValueError, match=r"arguments\.query: must not be blank"):
        mcp_server._dispatch_tool(tmp_path, tool_name, {"query": blank})


@pytest.mark.parametrize("tool_name", ["memory_query", "code_query", "context_pack"])
def test_missing_required_query_is_rejected_before_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
) -> None:
    def unreachable(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("handler must not run when a required field is absent")

    monkeypatch.setattr(mcp_server, "query", unreachable)
    monkeypatch.setattr(mcp_server, "context_pack", unreachable)

    with pytest.raises(ValueError, match=r"arguments\.query: required"):
        mcp_server._dispatch_tool(tmp_path, tool_name, {"limit": 5})


def test_blank_sandbox_command_is_rejected() -> None:
    assert mcp_server._validate_tool_arguments(
        "sandbox_execute",
        {"command": "   "},
    ) == "arguments.command: must not be blank"


def test_every_required_string_field_publishes_min_length() -> None:
    """tools/list must advertise the constraint the validator enforces."""
    offenders: list[str] = []
    for tool in mcp_server.TOOLS:
        schema = tool.get("inputSchema") or {}
        properties = schema.get("properties") or {}
        for key in schema.get("required") or []:
            prop = properties.get(key)
            if not isinstance(prop, dict) or prop.get("type") != "string":
                continue
            if prop.get("minLength", 0) < 1:
                offenders.append(f"{tool['name']}.{key}")
    assert offenders == []


def test_blank_required_string_rejected_across_whole_catalog(tmp_path: Path) -> None:
    """No tool in the catalog may accept a blank value for a required string field."""
    accepted: list[str] = []
    for tool in mcp_server.TOOLS:
        schema = tool.get("inputSchema") or {}
        properties = schema.get("properties") or {}
        required = [
            key
            for key in schema.get("required") or []
            if isinstance(properties.get(key), dict) and properties[key].get("type") == "string"
        ]
        if not required:
            continue
        payload = {key: "" for key in schema.get("required") or []}
        error = mcp_server._validate_tool_arguments(str(tool["name"]), payload)
        if error is None or "must not be blank" not in error:
            accepted.append(str(tool["name"]))
    assert accepted == []


def test_non_required_empty_string_still_allowed() -> None:
    """Optional free-text fields keep accepting empty values."""
    assert mcp_server._validate_tool_arguments(
        "evidence_record",
        {"query": "needle", "path": "src/a.py", "note": ""},
    ) is None


def test_repeated_identical_rejection_escalates_to_a_stop_order(tmp_path: Path) -> None:
    """The Nth identical bad call must read as "stop", not as another retryable error."""
    payload = {"query": ""}

    for _attempt in range(mcp_server.MCP_REPEATED_REJECTION_LIMIT - 1):
        with pytest.raises(ValueError) as early:
            mcp_server._dispatch_tool(tmp_path, "code_query", payload)
        assert "stop retrying" not in str(early.value)

    with pytest.raises(ValueError, match=r"stop retrying this call") as final:
        mcp_server._dispatch_tool(tmp_path, "code_query", payload)
    assert "rejected 3x" in str(final.value)


def test_loop_guard_counts_each_argument_shape_separately(tmp_path: Path) -> None:
    """Distinct bad payloads are distinct mistakes, not one loop."""
    for index in range(mcp_server.MCP_REPEATED_REJECTION_LIMIT + 2):
        with pytest.raises(ValueError) as exc_info:
            mcp_server._dispatch_tool(tmp_path, "code_query", {"query": "", "limit": index})
        assert "stop retrying" not in str(exc_info.value)


def test_loop_guard_resets_after_a_valid_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mcp_server,
        "query",
        lambda *_args, **_kwargs: {"ok": True, "results": []},
    )
    for _attempt in range(mcp_server.MCP_REPEATED_REJECTION_LIMIT - 1):
        with pytest.raises(ValueError):
            mcp_server._dispatch_tool(tmp_path, "code_query", {"query": ""})

    mcp_server._dispatch_tool(tmp_path, "code_query", {"query": "needle"})

    with pytest.raises(ValueError) as exc_info:
        mcp_server._dispatch_tool(tmp_path, "code_query", {"query": ""})
    assert "stop retrying" not in str(exc_info.value)


def test_loop_guard_tracking_table_stays_bounded(tmp_path: Path) -> None:
    for index in range(mcp_server.MCP_REPEATED_REJECTION_TRACKED_KEYS * 2):
        with pytest.raises(ValueError):
            mcp_server._dispatch_tool(tmp_path, "code_query", {"query": "", "limit": index})

    assert (
        len(mcp_server._REPEATED_REJECTIONS)
        <= mcp_server.MCP_REPEATED_REJECTION_TRACKED_KEYS
    )


def test_unknown_tool_still_gets_global_shape_validation() -> None:
    error = mcp_server._validate_tool_arguments(
        "unknown_tool",
        {"payload": _nested_mapping(mcp_server.MCP_ARGUMENT_MAX_DEPTH + 2)},
    )

    assert error is not None
    assert error.endswith("nesting too deep")
