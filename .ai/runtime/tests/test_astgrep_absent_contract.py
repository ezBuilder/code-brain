"""Absent/disabled ast-grep must degrade to [] without raising.

Runs regardless of whether the binary is installed, using AI_ASTGREP_DISABLE
to force the absent path so CI without ast-grep and CI with it both cover this.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_core import astgrep_integration as ag

EXTRACTORS = [
    "extract_symbols_js", "extract_calls_js",
    "extract_symbols_ts", "extract_calls_ts",
    "extract_symbols_go", "extract_calls_go",
    "extract_symbols_rs", "extract_calls_rs",
    "extract_symbols_kt", "extract_calls_kt",
    "extract_symbols_dart", "extract_calls_dart",
]


@pytest.mark.parametrize("name", EXTRACTORS)
def test_disabled_returns_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv("AI_ASTGREP_DISABLE", "1")
    target = tmp_path / "sample.rs"
    target.write_text("fn a() { b(); }\n", encoding="utf-8")
    assert getattr(ag, name)(str(target)) == []


@pytest.mark.parametrize("name", EXTRACTORS)
def test_missing_file_returns_empty(tmp_path: Path, name: str) -> None:
    assert getattr(ag, name)(str(tmp_path / "does-not-exist.rs")) == []


@pytest.mark.parametrize("name", EXTRACTORS)
def test_directory_path_returns_empty(tmp_path: Path, name: str) -> None:
    assert getattr(ag, name)(str(tmp_path)) == []


def test_every_wired_language_has_a_spec() -> None:
    """search.py maps extensions to these extractors; each needs a kind spec."""
    assert set(ag._SYMBOL_SPECS) == {
        "JavaScript", "TypeScript", "Go", "Rust", "Kotlin", "Dart"
    }
    for language, spec in ag._SYMBOL_SPECS.items():
        assert spec["symbol_kinds"], language
        assert spec["name_re"], language
