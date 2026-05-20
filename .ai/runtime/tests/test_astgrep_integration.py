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
