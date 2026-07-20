from __future__ import annotations

from pathlib import Path

import pytest

from ai_core import codegraph as cg
from ai_core import mcp_server
from ai_core import search as search_mod


def _seed_many(root: Path, *, count: int = 140) -> None:
    with search_mod._connection_scope(root) as conn:
        search_mod.init_schema(conn)
        conn.executemany(
            "insert into code_calls(path, caller, callee, lineno, lang) values(?,?,?,?,?)",
            [
                ("calls.py", f"caller_{index:03d}", "target", index + 1, "python")
                for index in range(count)
            ],
        )
        conn.executemany(
            "insert into code_symbols(path, qualname, kind, lineno, end_lineno, parent, lang) "
            "values(?,?,?,?,?,?,?)",
            [
                ("symbols.py", f"symbol_{index:03d}", "function", index + 1, index + 1, "", "python")
                for index in range(count)
            ]
            + [
                ("symbols.py", "literal%needle", "function", 1000, 1000, "", "python"),
                ("symbols.py", "literalXneedle", "function", 1001, 1001, "", "python"),
                ("symbols.py", "literal_needle", "function", 1002, 1002, "", "python"),
            ],
        )


def test_graph_query_limits_cannot_become_unlimited(tmp_path: Path) -> None:
    _seed_many(tmp_path)

    negative = cg.query_callers(tmp_path, "target", limit=-1)
    oversized = cg.query_callers(tmp_path, "target", limit=10**9)
    symbols = cg.find_symbol(tmp_path, "symbol_", limit=10**9)
    hotspots = cg.hotspot_callees(tmp_path, limit=10**9)

    assert negative["count"] == 1
    assert oversized["count"] == search_mod.SEARCH_RESULT_MAX
    assert symbols["count"] == search_mod.SEARCH_RESULT_MAX
    assert hotspots["count"] <= search_mod.SEARCH_RESULT_MAX


@pytest.mark.parametrize(
    ("needle", "expected"),
    [
        ("%", "literal%needle"),
        ("_", "literal_needle"),
    ],
)
def test_symbol_fragment_treats_like_metacharacters_as_literals(
    tmp_path: Path,
    needle: str,
    expected: str,
) -> None:
    _seed_many(tmp_path, count=1)

    payload = cg.find_symbol(tmp_path, needle, limit=20)
    names = {item["qualname"] for item in payload["symbols"]}

    assert expected in names
    if needle == "%":
        assert "literalXneedle" not in names


@pytest.mark.parametrize("value", ["", "\x00", "x" * (cg.GRAPH_QUERY_MAX_CHARS + 1)])
def test_graph_queries_reject_invalid_text_before_database_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setattr(
        search_mod,
        "_connection_scope",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid graph query must not open the index")
        ),
    )

    callers = cg.query_callers(tmp_path, value)
    callees = cg.query_callees(tmp_path, value)
    symbols = cg.find_symbol(tmp_path, value)

    assert callers["ok"] is False
    assert callees["ok"] is False
    assert symbols["ok"] is False


def test_graph_depth_seed_and_result_normalizers_are_bounded(tmp_path: Path) -> None:
    _seed_many(tmp_path)
    seeds = [f"seed_{index}" for index in range(cg.GRAPH_MAX_SEEDS + 20)]

    impact = cg.blast_radius(
        tmp_path,
        symbols=seeds,
        max_depth=10**9,
        limit=10**9,
    )

    assert cg._normalize_depth(10**9, default=4) == cg.GRAPH_MAX_DEPTH
    assert cg._normalize_depth(-5, default=4) == 1
    assert len(impact["seeds"]) == cg.GRAPH_MAX_SEEDS
    assert len(impact["impacted"]) <= search_mod.SEARCH_RESULT_MAX


def test_mcp_graph_schemas_publish_runtime_bounds() -> None:
    by_name = {tool["name"]: tool for tool in mcp_server.TOOLS}
    for name in ("code_graph_callers", "code_graph_callees", "code_graph_symbol"):
        props = by_name[name]["inputSchema"]["properties"]
        assert props["limit"]["minimum"] == 1
        assert props["limit"]["maximum"] == search_mod.SEARCH_RESULT_MAX
    trace_props = by_name["code_graph_trace"]["inputSchema"]["properties"]
    assert trace_props["max_depth"]["maximum"] == cg.GRAPH_MAX_DEPTH
    impact_props = by_name["code_graph_impact"]["inputSchema"]["properties"]
    assert impact_props["paths"]["maxItems"] == cg.GRAPH_MAX_SEEDS
    assert impact_props["symbols"]["maxItems"] == cg.GRAPH_MAX_SEEDS
