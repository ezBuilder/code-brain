"""codegraph — Python AST extractor regression tests."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core.codegraph import (  # noqa: E402
    extract_symbols,
    extract_calls,
    extract_references,
    iter_python_files,
    query_references,
    Symbol,
    CallEdge,
    Reference,
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
    edge = matched[0]
    assert edge.target == "C.helper"
    assert edge.resolution == "class_member"
    assert edge.confidence == 0.95


def test_extract_calls_resolves_import_aliases_with_provenance():
    src = '''
from pkg.service import helper as h
import os.path as osp

def run():
    h()
    osp.join("a", "b")
'''
    edges = {edge.lexical_callee: edge for edge in extract_calls(src, path="src/app.py")}

    imported = edges["h"]
    assert imported.callee == "helper"
    assert imported.target == "pkg.service.helper"
    assert imported.resolution == "from_import_alias"
    assert imported.confidence == 0.95

    module_alias = edges["osp.join"]
    assert module_alias.callee == "osp.join"
    assert module_alias.target == "os.path.join"
    assert module_alias.resolution == "import_alias"
    assert module_alias.confidence == 0.9


def test_extract_calls_resolves_relative_import_and_nested_symbol():
    src = '''
from .service import Worker as W

def outer():
    def inner():
        return 1
    inner()
    W.run()
'''
    edges = {edge.lexical_callee: edge for edge in extract_calls(src, path="pkg/worker.py")}

    assert edges["inner"].callee == "outer.inner"
    assert edges["inner"].resolution == "same_file_symbol"
    assert edges["W.run"].callee == "Worker.run"
    assert edges["W.run"].target == "pkg.service.Worker.run"
    assert edges["W.run"].resolution == "relative_import_alias"


def test_function_local_import_overrides_module_alias():
    src = '''
import alpha as service

def run():
    import beta as service
    service.execute()
'''
    edge = next(edge for edge in extract_calls(src, path="app.py") if edge.lexical_callee == "service.execute")
    assert edge.target == "beta.execute"
    assert edge.resolution == "import_alias"


def test_plain_method_call_does_not_resolve_through_class_namespace():
    src = '''
def helper():
    return 1

class Worker:
    def helper(self):
        return 2

    def run(self):
        return helper()
'''
    edge = next(edge for edge in extract_calls(src, path="worker.py") if edge.caller == "Worker.run")
    assert edge.lexical_callee == "helper"
    assert edge.callee == "helper"
    assert edge.target == "helper"
    assert edge.resolution == "same_file_symbol"


def test_extract_references_indexes_non_call_reads_and_exact_ranges():
    src = '''from pkg.service import helper as h
def run(worker):
    callback = h
    result = h()
    return worker.finish
'''
    references = extract_references(src, path="app.py")

    binding = next(item for item in references if item.kind == "import_binding")
    assert binding.scope == "<module>"
    assert binding.name == "h"
    assert binding.lexical_name == "h"
    assert binding.target == "pkg.service.helper"
    assert binding.resolution == "from_import_alias"

    callback = next(
        item
        for item in references
        if item.kind == "name_read" and item.lexical_name == "h"
    )
    assert isinstance(callback, Reference)
    assert callback.scope == "run"
    assert callback.name == "helper"
    assert callback.target == "pkg.service.helper"
    assert callback.resolution == "from_import_alias"
    assert (callback.lineno, callback.column, callback.end_lineno, callback.end_column) == (3, 15, 3, 16)

    call = next(item for item in references if item.kind == "call")
    assert call.lexical_name == "h"
    assert call.target == "pkg.service.helper"
    assert (call.lineno, call.column, call.end_lineno, call.end_column) == (4, 13, 4, 14)

    attribute = next(item for item in references if item.kind == "attribute_read")
    assert attribute.lexical_name == "worker.finish"
    assert attribute.target is None
    assert attribute.resolution == "lexical"
    assert (attribute.lineno, attribute.column, attribute.end_lineno, attribute.end_column) == (5, 11, 5, 24)

    # A call target and a full attribute are each indexed once, without the
    # duplicate base-name rows produced by a naive ast.walk traversal.
    assert [(item.kind, item.lexical_name) for item in references].count(("call", "h")) == 1
    assert not any(item.lexical_name == "worker" for item in references)


def test_extract_references_resolves_self_member_for_read_and_call():
    src = '''class Worker:
    def run(self):
        callback = self.finish
        return self.finish()

    def finish(self):
        return 1
'''
    references = extract_references(src, path="worker.py")
    member_refs = [item for item in references if item.lexical_name == "self.finish"]

    assert {item.kind for item in member_refs} == {"attribute_read", "call"}
    assert all(item.scope == "Worker.run" for item in member_refs)
    assert all(item.name == "self.finish" for item in member_refs)
    assert all(item.target == "Worker.finish" for item in member_refs)
    assert all(item.resolution == "class_member" for item in member_refs)
    assert all(item.confidence == 0.95 for item in member_refs)
    assert not any(item.lexical_name == "self" for item in references)


def test_extract_references_honours_function_local_import_shadowing():
    src = '''import alpha as service
def run():
    import beta as service
    callback = service.execute
'''
    reference = next(
        item
        for item in extract_references(src, path="app.py")
        if item.kind == "attribute_read"
    )
    assert reference.lexical_name == "service.execute"
    assert reference.target == "beta.execute"
    assert reference.resolution == "import_alias"


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
    assert callers["backend"] == "syntactic_codegraph"
    assert all("resolution" in caller and "confidence" in caller for caller in callers["callers"])
    callees = query_callees(tmp_path, "alpha", limit=10)
    callee_names = {c["callee"] for c in callees["callees"]}
    assert "helper" in callee_names
    syms = find_symbol(tmp_path, "alpha", limit=10)
    assert any(s["qualname"] == "alpha" for s in syms["symbols"])
    hot = hotspot_callees(tmp_path, limit=10)
    hot_names = {h["callee"] for h in hot["hotspots"]}
    assert "helper" in hot_names


def test_query_callers_matches_resolved_target_and_reports_match_source(tmp_path: Path):
    from ai_core.codegraph import query_callers
    from ai_core.search import rebuild

    src = tmp_path / "src" / "consumer.py"
    src.parent.mkdir(parents=True)
    src.write_text(
        "from pkg.service import helper as h\n\ndef run():\n    h()\n",
        encoding="utf-8",
    )
    (tmp_path / ".ai" / "cache").mkdir(parents=True)
    rebuild(tmp_path)

    result = query_callers(tmp_path, "pkg.service.helper", limit=10)

    assert result["count"] == 1
    call = result["callers"][0]
    assert call["lexical_callee"] == "h"
    assert call["callee"] == "helper"
    assert call["target"] == "pkg.service.helper"
    assert call["matched_on"] == "target"
    assert call["resolution"] == "from_import_alias"


def test_query_references_includes_non_call_uses_and_definition_ambiguity(tmp_path: Path):
    from ai_core.search import rebuild

    for rel, content in {
        "pkg/a.py": "def helper():\n    return 1\n",
        "pkg/b.py": "def helper():\n    return 2\n",
        "consumer.py": (
            "from pkg.a import helper as h\n\n"
            "def run():\n"
            "    callback = h\n"
            "    return h()\n"
        ),
    }.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (tmp_path / ".ai" / "cache").mkdir(parents=True)
    assert rebuild(tmp_path)["ok"] is True

    resolved = query_references(tmp_path, "pkg.a.helper", limit=10)

    assert resolved["backend"] == "syntactic_codegraph"
    assert resolved["precision"] == "syntactic"
    assert resolved["complete"] is True
    assert resolved["ambiguous"] is False
    assert resolved["definition_candidate_count"] == 2
    assert resolved["best_definition_count"] == 1
    assert [item["kind"] for item in resolved["references"]] == [
        "name_read",
        "call",
        "import_binding",
    ]
    assert all(item["path"] == "consumer.py" for item in resolved["references"])
    assert all(item["target"] == "pkg.a.helper" for item in resolved["references"])
    assert all(item["matched_on"] == "target" for item in resolved["references"])
    assert [(item["lineno"], item["column"]) for item in resolved["references"]] == [
        (4, 15),
        (5, 11),
        (1, 18),
    ]
    assert resolved["definition_candidates"][0]["canonical"] == "pkg.a.helper"

    ambiguous = query_references(tmp_path, "helper", limit=1)
    assert ambiguous["ambiguous"] is True
    assert ambiguous["best_definition_count"] == 2
    assert ambiguous["partial"] is True
    assert ambiguous["complete"] is False
