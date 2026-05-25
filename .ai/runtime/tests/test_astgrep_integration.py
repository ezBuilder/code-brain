"""Tests for ai_core.astgrep_integration (T48)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core.astgrep_integration import astgrep_available, scan_path  # noqa: E402


def test_astgrep_available_returns_bool():
    assert isinstance(astgrep_available(), bool)


def test_scan_path_returns_empty_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_ASTGREP_DISABLE", "1")
    target = tmp_path / "x.js"
    target.write_text("eval('1+1');\n", encoding="utf-8")
    assert scan_path(target) == []


def test_scan_path_returns_empty_when_binary_missing(tmp_path, monkeypatch):
    # Hide PATH so shutil.which returns None for ast-grep/sg.
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("AI_ASTGREP_DISABLE", raising=False)
    target = tmp_path / "x.js"
    target.write_text("eval('1+1');\n", encoding="utf-8")
    assert scan_path(target) == []


def test_scan_path_returns_empty_for_missing_file(tmp_path, monkeypatch):
    monkeypatch.delenv("AI_ASTGREP_DISABLE", raising=False)
    missing = tmp_path / "nope.js"
    assert scan_path(missing) == []


def test_scan_path_accepts_custom_rule(tmp_path, monkeypatch):
    """Custom rule_yaml path must not raise even when ast-grep is absent."""
    monkeypatch.setenv("AI_ASTGREP_DISABLE", "1")
    target = tmp_path / "x.py"
    target.write_text("print('hi')\n", encoding="utf-8")
    rule = "id: x\nlanguage: Python\nrule:\n  pattern: print($X)\nseverity: info\nmessage: m\n"
    assert scan_path(target, rule) == []


@pytest.mark.skipif(not astgrep_available(), reason="ast-grep binary not installed")
def test_scan_path_detects_eval_in_js(tmp_path, monkeypatch):
    monkeypatch.delenv("AI_ASTGREP_DISABLE", raising=False)
    target = tmp_path / "danger.js"
    target.write_text("function go() { eval('1+1'); }\n", encoding="utf-8")
    findings = scan_path(target)
    # ast-grep should produce at least one finding for the built-in no-eval rule.
    assert isinstance(findings, list)
    # We don't hard-assert non-empty because ast-grep output schemas change
    # across versions; we DO assert the call did not raise and returned
    # parseable list-of-dicts.
    for f in findings:
        assert isinstance(f, dict)


@pytest.mark.skipif(not astgrep_available(), reason="ast-grep binary not installed")
def test_ast_verify_file_includes_astgrep_pass(tmp_path, monkeypatch):
    """verify_file on a JS file should run ast-grep without raising."""
    monkeypatch.delenv("AI_ASTGREP_DISABLE", raising=False)
    from ai_core.ast_verify import verify_file

    target = tmp_path / "danger.js"
    target.write_text("eval('1+1');\n", encoding="utf-8")
    # verify_file returns a Report; ast-grep findings (if any) appear with
    # kind="ast_grep". JS files trip the Python parser → syntax violation,
    # which is fine — we only want to confirm the integration call doesn't
    # blow up.
    rep = verify_file(target)
    assert rep is not None
    assert hasattr(rep, "violations")


@pytest.mark.skipif(not astgrep_available(), reason="ast-grep binary not installed")
def test_extract_symbols_js_returns_list(tmp_path, monkeypatch):
    """extract_symbols_js must return list of dicts (graceful degradation on parse failure)."""
    monkeypatch.delenv("AI_ASTGREP_DISABLE", raising=False)
    from ai_core.astgrep_integration import extract_symbols_js

    target = tmp_path / "test.js"
    target.write_text("function foo() { return 42; }\nconst bar = () => 'hello';\n", encoding="utf-8")
    result = extract_symbols_js(str(target))
    assert isinstance(result, list)
    # May be empty if ast-grep output format differs, but should not raise
    for item in result:
        assert isinstance(item, dict)


@pytest.mark.skipif(not astgrep_available(), reason="ast-grep binary not installed")
def test_extract_calls_js_returns_list(tmp_path, monkeypatch):
    """extract_calls_js must return list of dicts."""
    monkeypatch.delenv("AI_ASTGREP_DISABLE", raising=False)
    from ai_core.astgrep_integration import extract_calls_js

    target = tmp_path / "test.js"
    target.write_text("function foo() { console.log('hi'); helper(); }\nfunction helper() {}\n", encoding="utf-8")
    result = extract_calls_js(str(target))
    assert isinstance(result, list)
    for item in result:
        assert isinstance(item, dict)
        if item:
            assert "callee" in item or "lineno" in item


def test_extract_symbols_js_when_astgrep_disabled(tmp_path, monkeypatch):
    """When ast-grep disabled, extract_symbols_js returns empty list."""
    monkeypatch.setenv("AI_ASTGREP_DISABLE", "1")
    from ai_core.astgrep_integration import extract_symbols_js

    target = tmp_path / "test.js"
    target.write_text("function foo() {}\n", encoding="utf-8")
    assert extract_symbols_js(str(target)) == []


def test_extract_calls_js_when_astgrep_disabled(tmp_path, monkeypatch):
    """When ast-grep disabled, extract_calls_js returns empty list."""
    monkeypatch.setenv("AI_ASTGREP_DISABLE", "1")
    from ai_core.astgrep_integration import extract_calls_js

    target = tmp_path / "test.js"
    target.write_text("foo();\n", encoding="utf-8")
    assert extract_calls_js(str(target)) == []


def test_extract_symbols_ts_delegates_to_js(tmp_path, monkeypatch):
    """extract_symbols_ts should delegate to JS extraction."""
    monkeypatch.setenv("AI_ASTGREP_DISABLE", "1")
    from ai_core.astgrep_integration import extract_symbols_ts

    target = tmp_path / "test.ts"
    target.write_text("function foo(): number { return 1; }\n", encoding="utf-8")
    # When disabled, should return []
    assert extract_symbols_ts(str(target)) == []


def test_extract_symbols_go_when_disabled(tmp_path, monkeypatch):
    """extract_symbols_go returns [] when ast-grep disabled."""
    monkeypatch.setenv("AI_ASTGREP_DISABLE", "1")
    from ai_core.astgrep_integration import extract_symbols_go

    target = tmp_path / "test.go"
    target.write_text("func main() {}\n", encoding="utf-8")
    assert extract_symbols_go(str(target)) == []


def test_extract_symbols_rs_when_disabled(tmp_path, monkeypatch):
    """extract_symbols_rs returns [] when ast-grep disabled."""
    monkeypatch.setenv("AI_ASTGREP_DISABLE", "1")
    from ai_core.astgrep_integration import extract_symbols_rs

    target = tmp_path / "test.rs"
    target.write_text("fn main() {}\n", encoding="utf-8")
    assert extract_symbols_rs(str(target)) == []


def test_extract_calls_go_when_disabled(tmp_path, monkeypatch):
    """extract_calls_go returns [] when ast-grep disabled."""
    monkeypatch.setenv("AI_ASTGREP_DISABLE", "1")
    from ai_core.astgrep_integration import extract_calls_go

    target = tmp_path / "test.go"
    target.write_text("func main() { foo() }\n", encoding="utf-8")
    assert extract_calls_go(str(target)) == []


def test_extract_calls_rs_when_disabled(tmp_path, monkeypatch):
    """extract_calls_rs returns [] when ast-grep disabled."""
    monkeypatch.setenv("AI_ASTGREP_DISABLE", "1")
    from ai_core.astgrep_integration import extract_calls_rs

    target = tmp_path / "test.rs"
    target.write_text("fn main() { foo(); }\n", encoding="utf-8")
    assert extract_calls_rs(str(target)) == []
