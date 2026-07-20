from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from ai_core import lsp
from ai_core import mcp_server


@pytest.mark.parametrize(
    ("value", "default", "minimum", "maximum", "expected"),
    [
        ("12", 5, 1, 100, 12),
        ("invalid", 5, 1, 100, 5),
        ("9" * 1000, 5, 1, 100, 5),
        (True, 5, 1, 100, 5),
        (-100, 5, 1, 100, 1),
        (10**100, 5, 1, 100, 100),
        (3.0, 5, 1, 100, 3),
        (3.5, 5, 1, 100, 5),
    ],
)
def test_mcp_int_coercion_is_bounded_and_fail_soft(
    value,
    default: int,
    minimum: int,
    maximum: int,
    expected: int,
) -> None:
    assert mcp_server._coerce_int(
        value,
        default,
        minimum=minimum,
        maximum=maximum,
    ) == expected


@pytest.mark.parametrize(
    ("value", "default", "expected"),
    [
        ("0.75", 0.5, 0.75),
        ("nan", 0.5, 0.5),
        ("inf", 0.5, 0.5),
        ("x" * 1000, 0.5, 0.5),
        (True, 0.5, 0.5),
        (-10, 0.5, 0.0),
        (10, 0.5, 1.0),
    ],
)
def test_mcp_float_coercion_is_finite_and_bounded(
    value,
    default: float,
    expected: float,
) -> None:
    assert mcp_server._coerce_float(
        value,
        default,
        minimum=0.0,
        maximum=1.0,
    ) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        (1, True),
        (0, False),
        ("true", True),
        ("YES", True),
        ("false", False),
        ("off", False),
        ("unexpected", False),
        ([], False),
    ],
)
def test_mcp_bool_coercion_does_not_treat_nonempty_false_string_as_true(
    value,
    expected: bool,
) -> None:
    assert mcp_server._coerce_bool(value) is expected


def test_memory_query_dispatch_defaults_malformed_limit(
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
        {"query": "needle", "limit": "9" * 1000},
    )

    assert payload["ok"] is True
    assert captured["limit"] == 5


def test_obs_usage_dispatch_parses_boolean_strings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[bool] = []

    def fake_usage(_root, *, include_sessions):
        captured.append(include_sessions)
        return {"ok": True}

    monkeypatch.setattr(mcp_server, "usage_report", fake_usage)

    mcp_server._dispatch_tool(tmp_path, "obs_usage", {"include_sessions": "false"})
    mcp_server._dispatch_tool(tmp_path, "obs_usage", {"include_sessions": "true"})

    assert captured == [False, True]


def test_lsp_dispatch_forwards_malformed_position_to_runtime_validator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_find(root, file_path, line, column):
        captured.update(root=root, file_path=file_path, line=line, column=column)
        return {"ok": False, "reason": "invalid_position", "references": []}

    monkeypatch.setattr(lsp, "find_references", fake_find)

    payload = mcp_server._dispatch_tool(
        tmp_path,
        "code_find_references",
        {"file_path": "pkg/a.py", "line": "not-an-int", "column": "0"},
    )

    assert payload["reason"] == "invalid_position"
    assert captured["line"] == "not-an-int"
    assert captured["column"] == "0"


def test_sandbox_dispatch_bounds_timeout_and_boolean_strings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_execute(root, **kwargs):
        captured.update(root=root, **kwargs)
        return {"ok": True}

    monkeypatch.setattr(mcp_server, "sandbox_execute", fake_execute)

    payload = mcp_server._dispatch_tool(
        tmp_path,
        "sandbox_execute",
        {
            "command": ["echo", "ok"],
            "timeout": "9" * 1000,
            "isolate_network": "false",
            "isolate_env": "true",
        },
    )

    assert payload["ok"] is True
    assert captured["timeout"] == 30
    assert captured["isolate_network"] is False
    assert captured["isolate_env"] is True


def test_tool_search_dispatch_handles_malformed_limit_without_exception(tmp_path: Path) -> None:
    payload = mcp_server._dispatch_tool(
        tmp_path,
        "tool_search",
        {"query": "search", "limit": "not-a-number"},
    )

    assert payload["ok"] is True
    assert 1 <= len(payload["tools"]) <= 8


def test_dispatch_has_no_eager_builtin_scalar_coercion_from_args() -> None:
    source = inspect.getsource(mcp_server._dispatch_tool)
    assert re.search(r"(?<![A-Za-z0-9_])int\(args\.get", source) is None
    assert re.search(r"(?<![A-Za-z0-9_])float\(args\.get", source) is None
    assert re.search(r"(?<![A-Za-z0-9_])bool\(args\.get", source) is None
