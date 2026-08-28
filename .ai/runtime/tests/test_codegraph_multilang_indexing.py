"""End-to-end: indexing a repo must populate code_symbols/code_calls per language.

Covers the search.py extension->extractor wiring for Kotlin and Dart (added
after the graph layer was found to cover ~0% of blurivo's Dart and fluxwright's
Rust) alongside the pre-existing JS/TS/Go/Rust wiring.
"""
from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from ai_core import astgrep_integration as ag
from ai_core.search import db_path, rebuild

pytestmark = pytest.mark.skipif(
    not ag.astgrep_available(), reason="ast-grep binary not installed"
)

FILES = {
    "lib/a.dart": "class F {\n  int bar(int a) => baz(a);\n}\nvoid baz(int a) {}\n",
    "app/b.kt": "fun alpha(x: Int): Int = beta(x)\nfun beta(y: Int): Int { return y }\n",
    "src/c.rs": "fn one() -> u32 { 1 }\nfn two(a: u32, b: u32) -> u32 { one() + a + b }\n",
    "src/d.go": "package main\nfunc Alpha() int { return beta() }\nfunc beta() int { return 1 }\n",
    "web/e.ts": "export function alpha(): number { return beta(); }\nfunction beta() { return 1; }\n",
    "mod.py": "def f():\n    return g()\n\ndef g():\n    return 1\n",
}


def _init_repo(root: Path) -> None:
    for rel, body in FILES.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )


def test_index_populates_symbols_for_every_wired_language(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    result = rebuild(tmp_path)
    assert result["ok"] is True

    conn = sqlite3.connect(db_path(tmp_path))
    try:
        symbol_langs = dict(
            conn.execute("select lang, count(*) from code_symbols group by lang").fetchall()
        )
        call_langs = dict(
            conn.execute("select lang, count(*) from code_calls group by lang").fetchall()
        )
        names_by_lang: dict[str, set[str]] = {}
        for lang, qualname in conn.execute("select lang, qualname from code_symbols"):
            names_by_lang.setdefault(lang, set()).add(qualname)
    finally:
        conn.close()

    expected = {"python", "dart", "kotlin", "rust", "go", "typescript"}
    assert expected <= set(symbol_langs), f"missing symbol langs: {expected - set(symbol_langs)}"
    assert expected <= set(call_langs), f"missing call langs: {expected - set(call_langs)}"

    # Spot-check real identifiers, not just non-zero counts.
    assert {"bar", "baz"} <= names_by_lang["dart"]
    assert {"alpha", "beta"} <= names_by_lang["kotlin"]
    # Arity independence: `one` takes no args, `two` takes two.
    assert {"one", "two"} <= names_by_lang["rust"]


def test_doctor_reports_covered_after_multilang_index(tmp_path: Path) -> None:
    from ai_core.doctor import check_codegraph_coverage

    _init_repo(tmp_path)
    rebuild(tmp_path)
    check = check_codegraph_coverage(tmp_path)
    assert check.ok is True
    assert "degraded" not in check.detail, check.detail
    assert check.detail.startswith("ok covered="), check.detail


CALLER_FILES = {
    "a.dart": "class F {\n  int bar(int a) {\n    return baz(a);\n  }\n}\nvoid baz(int a) {\n  print(a);\n}\n",
    "b.rs": "fn one() -> u32 { 1 }\nfn two() -> u32 {\n    one()\n}\n",
    "c.kt": "fun alpha(x: Int): Int {\n    return beta(x)\n}\nfun beta(y: Int): Int { return y }\n",
    "d.ts": "function inner() { return 1; }\nexport function outer() {\n  return inner();\n}\n",
    "e.go": "package main\nfunc inner() int { return 1 }\nfunc outer() int {\n\treturn inner()\n}\n",
}


def test_calls_are_attributed_to_enclosing_function(tmp_path: Path) -> None:
    """Non-Python callers were hardcoded "<module>", making code_graph_callers
    unable to answer "which function calls this?" for any ast-grep language."""
    for rel, body in CALLER_FILES.items():
        (tmp_path / rel).write_text(body, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=tmp_path,
        check=True,
    )
    assert rebuild(tmp_path)["ok"] is True

    conn = sqlite3.connect(db_path(tmp_path))
    try:
        edges = {
            (path, caller, callee)
            for path, caller, callee in conn.execute(
                "select path, caller, callee from code_calls"
            )
        }
    finally:
        conn.close()

    assert ("b.rs", "two", "one") in edges
    assert ("c.kt", "alpha", "beta") in edges
    assert ("d.ts", "outer", "inner") in edges
    assert ("e.go", "outer", "inner") in edges
    # Dart signatures are header-only; the body-widening pass must still attribute.
    assert ("a.dart", "bar", "baz") in edges

    module_only = {e for e in edges if e[1] == "<module>"}
    assert not module_only, f"unattributed calls remain: {module_only}"


def test_dart_symbol_span_covers_body(tmp_path: Path) -> None:
    """Header-only Dart spans must widen, else callers collapse to <module>."""
    target = tmp_path / "s.dart"
    target.write_text(
        "class F {\n  int bar(int a) {\n    return 1;\n  }\n}\nint top() => 3;\n",
        encoding="utf-8",
    )
    by_name = {s["qualname"]: s for s in ag.extract_symbols_dart(str(target))}
    assert by_name["bar"]["end_lineno"] >= 4, by_name["bar"]
    assert by_name["top"]["lineno"] == 6
