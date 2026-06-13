"""Call-path tracing, blast radius, architecture summary (landscape P6)."""
from __future__ import annotations

from pathlib import Path

from ai_core import codegraph as cg
from ai_core import mcp_server as m
from ai_core.search import connect, init_schema


def _seed_graph(root: Path) -> None:
    (root / ".ai" / "cache").mkdir(parents=True, exist_ok=True)
    with connect(root) as conn:
        init_schema(conn)
        conn.executemany(
            "insert into code_calls(path, caller, callee, lineno, lang) values(?,?,?,?,?)",
            [("a.py", "a", "b", 1, "python"),
             ("a.py", "b", "c", 2, "python"),
             ("a.py", "d", "b", 3, "python")],
        )
        conn.executemany(
            "insert into code_symbols(path, qualname, kind, lineno, end_lineno, parent, lang) "
            "values(?,?,?,?,?,?,?)",
            [("a.py", "a", "function", 1, 5, "", "python"),
             ("a.py", "b", "function", 6, 9, "", "python")],
        )
        conn.commit()


def test_trace_finds_multihop_path(tmp_path: Path) -> None:
    _seed_graph(tmp_path)
    r = cg.trace_call_path(tmp_path, src="a", dst="c")
    assert r["found"] is True and r["path"] == ["a", "b", "c"]


def test_trace_no_path(tmp_path: Path) -> None:
    _seed_graph(tmp_path)
    r = cg.trace_call_path(tmp_path, src="c", dst="a")
    assert r["found"] is False


def test_blast_radius_reverse(tmp_path: Path) -> None:
    _seed_graph(tmp_path)
    r = cg.blast_radius(tmp_path, symbols=["b"])
    callers = {x["symbol"] for x in r["impacted"]}
    assert "a" in callers and "d" in callers  # both call b


def test_impact_by_paths(tmp_path: Path) -> None:
    _seed_graph(tmp_path)
    r = cg.impacted_by_paths(tmp_path, paths=["a.py"])
    assert r["changed_symbols"] >= 1 and r["ok"] is True


def test_architecture_summary(tmp_path: Path) -> None:
    _seed_graph(tmp_path)
    r = cg.architecture_summary(tmp_path)
    assert r["ok"] and any(mod["module"] == "a.py" for mod in r["modules"])


def test_mcp_graph_tools_dispatch(tmp_path: Path) -> None:
    _seed_graph(tmp_path)
    assert m._dispatch_tool(tmp_path, "code_graph_trace", {"src": "a", "dst": "c"})["found"]
    assert m._dispatch_tool(tmp_path, "code_graph_impact", {"symbols": ["b"]})["ok"]
    assert m._dispatch_tool(tmp_path, "code_graph_architecture", {})["ok"]
