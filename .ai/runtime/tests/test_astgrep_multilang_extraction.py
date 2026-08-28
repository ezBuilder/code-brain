"""Regression tests for multi-language symbol/call extraction via ast-grep.

Two real bugs motivated these tests, both of which made the graph layer a
silent no-op for every non-Python language even with ast-grep installed:

  1. Rules used `pattern: fn $FUNC($_) { ... }`. `$_` matches exactly ONE node,
     so zero-arg and multi-arg functions never matched.
  2. Extractors read `finding["matches"][*]["start"]`, but ast-grep
     `--json=stream` emits one object per match with the span under
     `range.start` / `range.end` and no `matches` key at all.

Tests are skipped when the optional ast-grep binary is absent; the
absent-binary contract is asserted separately without needing the binary.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_core import astgrep_integration as ag

pytestmark = pytest.mark.skipif(
    not ag.astgrep_available(), reason="ast-grep binary not installed"
)

# (language suffix, source, expected symbol names, expected callee names)
CASES = [
    (
        "rs",
        """\
fn zero() -> u32 { 7 }
fn one(a: u32) -> u32 { a + 1 }
fn many(a: u32, b: u32, c: u32) -> u32 { one(a) + b + c }
struct S;
impl S { fn method(&self, x: u32) -> u32 { many(x, 1, 2) } }
""",
        {"zero", "one", "many", "method"},
        {"one", "many"},
    ),
    (
        "dart",
        """\
class Foo {
  int bar(int a) => baz(a) + 1;
  void qux() { print(bar(1)); }
}
void baz(int a) { print(a); }
int topLevel() => 3;
""",
        {"bar", "qux", "baz", "topLevel"},
        {"baz", "bar", "print"},
    ),
    (
        "kt",
        """\
fun zero(): Int = 1
fun alpha(x: Int): Int = beta(x)
fun beta(y: Int): Int { return y + 1 }
class K { fun gamma(n: Int): Int { return alpha(n) } }
""",
        {"zero", "alpha", "beta", "gamma"},
        {"beta", "alpha"},
    ),
    (
        "go",
        """\
package main
func Zero() int { return 0 }
func Alpha(x int) int { return beta(x) }
func beta(y int) int { return y }
type T struct{}
func (t T) Method(n int) int { return Alpha(n) }
""",
        {"Zero", "Alpha", "beta", "Method"},
        {"beta", "Alpha"},
    ),
    (
        "ts",
        """\
export function alpha(x: number): number { return beta(x); }
function beta(y: number) { return y; }
const gamma = (z: number): number => beta(z);
class K { delta(n: number) { return alpha(n); } }
""",
        {"alpha", "beta", "gamma", "delta", "K"},
        {"beta", "alpha"},
    ),
]


@pytest.mark.parametrize("suffix,source,expected_symbols,expected_calls", CASES)
def test_extracts_symbols_and_calls(
    tmp_path: Path, suffix: str, source: str, expected_symbols: set[str], expected_calls: set[str]
) -> None:
    target = tmp_path / f"sample.{suffix}"
    target.write_text(source, encoding="utf-8")

    symbols = getattr(ag, f"extract_symbols_{suffix}")(str(target))
    calls = getattr(ag, f"extract_calls_{suffix}")(str(target))

    names = {s["qualname"] for s in symbols}
    assert expected_symbols <= names, f"missing symbols: {expected_symbols - names}"
    callees = {c["callee"] for c in calls}
    assert expected_calls <= callees, f"missing callees: {expected_calls - callees}"

    # Arity independence: the historical bug dropped zero-arg and multi-arg
    # declarations. Assert every declaration is present, not merely some.
    assert len(names) >= len(expected_symbols)

    for record in symbols:
        assert record["lineno"] >= 1, "lines must be 1-indexed"
        assert record["end_lineno"] >= record["lineno"]
        assert record["kind"] in {"function", "class"}
    for record in calls:
        assert record["lineno"] >= 1
        assert record["callee"].isidentifier()


def test_span_reader_uses_range_not_matches() -> None:
    """Guard against reintroducing the `finding["matches"]` misread."""
    finding = {"range": {"start": {"line": 0, "column": 0}, "end": {"line": 4, "column": 1}}}
    assert ag._finding_span(finding) == (1, 5)
    # A payload shaped like the OLD wrong assumption yields no span.
    assert ag._finding_span({"matches": [{"start": {"line": 3}}]}) is None
    assert ag._finding_span({}) is None


def test_kind_rule_builds_any_clause_for_multiple_kinds() -> None:
    single = ag._kind_rule("Rust", ("function_item",))
    assert "kind: function_item" in single
    assert "any:" not in single
    multi = ag._kind_rule("Go", ("function_declaration", "method_declaration"))
    assert "any:" in multi
    assert "- kind: function_declaration" in multi
    assert "- kind: method_declaration" in multi


def test_callee_keeps_final_segment_only(tmp_path: Path) -> None:
    """`self.foo()` and `mod::foo()` must join on `foo`, like the Python extractor."""
    target = tmp_path / "q.rs"
    target.write_text("fn caller() { helper::inner(1); }\nfn other() { }\n", encoding="utf-8")
    calls = ag.extract_calls_rs(str(target))
    assert "inner" in {c["callee"] for c in calls}


def test_function_valued_variables_are_symbols(tmp_path: Path) -> None:
    """`const f = () => {}` is the dominant declaration style in modern JS/TS.

    Matching only function/method node kinds missed it entirely: fluxwright's
    ui/app.js declares every function this way and yielded 0 symbols.
    """
    target = tmp_path / "arrow.js"
    target.write_text(
        "const notify = (m) => { console.log(m); };\n"
        "let handler = function (e) { return e; };\n"
        "export const exported = (x) => x * 2;\n"
        "async function classic() { return 1; }\n"
        "class C { m() {} }\n",
        encoding="utf-8",
    )
    names = {s["qualname"] for s in ag.extract_symbols_js(str(target))}
    assert {"notify", "handler", "exported", "classic", "C", "m"} <= names


def test_plain_data_variables_are_not_symbols(tmp_path: Path) -> None:
    """The `has:` constraint must keep non-callable declarations out."""
    target = tmp_path / "data.js"
    target.write_text(
        "const count = 1;\n"
        "const label = 'x';\n"
        "const list = [1, 2, 3];\n"
        "const cfg = { a: 1 };\n"
        "const real = () => 1;\n",
        encoding="utf-8",
    )
    names = {s["qualname"] for s in ag.extract_symbols_js(str(target))}
    assert "real" in names
    assert not ({"count", "label", "list", "cfg"} & names), f"data captured: {names}"


def test_class_kind_is_distinguished_from_function(tmp_path: Path) -> None:
    target = tmp_path / "k.ts"
    target.write_text("class Widget { run() {} }\nfunction helper() {}\n", encoding="utf-8")
    by_name = {s["qualname"]: s["kind"] for s in ag.extract_symbols_ts(str(target))}
    assert by_name.get("Widget") == "class"
    assert by_name.get("helper") == "function"
    assert by_name.get("run") == "function"
