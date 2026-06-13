"""Agent-facing ast-grep structural search — input guards (binary-independent)."""
from __future__ import annotations

from pathlib import Path

from ai_core.astgrep_integration import ast_grep_search


def test_unsupported_lang(tmp_path: Path) -> None:
    r = ast_grep_search(tmp_path, pattern="x", lang="cobol")
    assert r["ok"] is False and "unsupported" in r["reason"]


def test_empty_pattern(tmp_path: Path) -> None:
    r = ast_grep_search(tmp_path, pattern="   ", lang="python")
    assert r["ok"] is False and "empty" in r["reason"]


def test_path_escape_blocked(tmp_path: Path) -> None:
    r = ast_grep_search(tmp_path, pattern="def $N($$$):", lang="py", path="../../../etc")
    assert r["ok"] is False and "escapes repo" in r["reason"]


def test_lang_alias_and_graceful_when_absent(tmp_path: Path) -> None:
    # valid inputs: either real matches (ok True) or graceful 'not installed', never raises
    r = ast_grep_search(tmp_path, pattern="def $N($$$):", lang="py")
    assert "ok" in r and isinstance(r.get("matches"), list)
