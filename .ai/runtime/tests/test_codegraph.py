"""codegraph — Python AST extractor regression tests."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core.codegraph import (  # noqa: E402
    extract_symbols,
    extract_calls,
    iter_python_files,
    Symbol,
    CallEdge,
)


def test_extract_symbols_basic():
    src = '''
def foo():
    pass

class Bar:
    def method(self):
        pass

    async def amethod(self):
        pass

async def baz():
    pass
'''
    syms = extract_symbols(src, path="m.py")
    qns = {s.qualname: s.kind for s in syms}
    assert qns["foo"] == "function"
    assert qns["baz"] == "async_function"
    assert qns["Bar"] == "class"
    assert qns["Bar.method"] == "method"
    assert qns["Bar.amethod"] == "async_method"


def test_extract_symbols_nested():
    src = '''
def outer():
    def inner():
        pass
    return inner
'''
    syms = extract_symbols(src, path="m.py")
    qns = {s.qualname for s in syms}
    assert "outer" in qns
    assert "outer.inner" in qns


def test_extract_calls_basic():
    src = '''
def caller():
    foo()
    self.bar()
    mod.baz()
'''
    edges = extract_calls(src, path="m.py")
    callees = sorted(e.callee for e in edges if e.caller == "caller")
    assert callees == ["foo", "mod.baz", "self.bar"]


def test_extract_calls_module_level():
    src = '''
import os
print(os.getcwd())
'''
    edges = extract_calls(src, path="m.py")
    module_callees = {e.callee for e in edges if e.caller == "<module>"}
    assert "print" in module_callees
    assert "os.getcwd" in module_callees


def test_extract_calls_class_methods():
    src = '''
class C:
    def m1(self):
        self.helper()

    def helper(self):
        pass
'''
    edges = extract_calls(src, path="m.py")
    # caller for `self.helper()` should be C.m1
    matched = [e for e in edges if e.callee == "self.helper"]
    assert any(e.caller == "C.m1" for e in matched)


def test_syntax_error_returns_empty():
    """Malformed source must not raise — indexer continues with other files."""
    assert extract_symbols("def foo(:", path="bad.py") == []
    assert extract_calls("def foo(:", path="bad.py") == []


def test_iter_python_files_skips_caches(tmp_path: Path):
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "a.py").write_text("x = 1\n")
    (tmp_path / "src" / "pkg" / "__pycache__").mkdir()
    (tmp_path / "src" / "pkg" / "__pycache__" / "a.cpython-311.pyc").write_text("")
    (tmp_path / ".git" / "config").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".git" / "config").write_text("")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "ignored.py").write_text("x = 2\n")
    found = list(iter_python_files(tmp_path))
    rels = {p.relative_to(tmp_path).as_posix() for p in found}
    assert "src/pkg/a.py" in rels
    assert not any(".git" in r or ".venv" in r or "__pycache__" in r for r in rels)


def test_symbol_loc_count():
    sym = Symbol(path="m.py", qualname="f", kind="function", lineno=10, end_lineno=15)
    assert sym.loc_count() == 6


def test_extract_symbols_on_real_codegraph_module():
    """Smoke: parse our own codegraph.py — should find extract_symbols itself."""
    from ai_core import codegraph
    src = Path(codegraph.__file__).read_text(encoding="utf-8")
    syms = extract_symbols(src, path="codegraph.py")
    qns = {s.qualname for s in syms}
    assert "extract_symbols" in qns
    assert "extract_calls" in qns
    assert "_walk_symbols" in qns


def test_query_functions_end_to_end(tmp_path: Path):
    """codegraph CLI helpers — exercise full indexing + retrieval cycle."""
    from ai_core.codegraph import (
        query_callers, query_callees, find_symbol, hotspot_callees,
    )
    from ai_core.search import rebuild

    src1 = tmp_path / "src" / "a.py"
    src1.parent.mkdir(parents=True)
    src1.write_text(
        "def alpha():\n    helper()\n    helper()\n\n"
        "def helper():\n    pass\n",
        encoding="utf-8",
    )
    src2 = tmp_path / "src" / "b.py"
    src2.write_text(
        "def beta():\n    alpha()\n    helper()\n",
        encoding="utf-8",
    )
    (tmp_path / ".ai" / "cache").mkdir(parents=True)
    rebuild(tmp_path)

    callers = query_callers(tmp_path, "helper", limit=10)
    assert callers["count"] >= 3
    callees = query_callees(tmp_path, "alpha", limit=10)
    callee_names = {c["callee"] for c in callees["callees"]}
    assert "helper" in callee_names
    syms = find_symbol(tmp_path, "alpha", limit=10)
    assert any(s["qualname"] == "alpha" for s in syms["symbols"])
    hot = hotspot_callees(tmp_path, limit=10)
    hot_names = {h["callee"] for h in hot["hotspots"]}
    assert "helper" in hot_names
