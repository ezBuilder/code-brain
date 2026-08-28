"""Regression tests for the codegraph_coverage doctor probe.

The graph layer (code_symbols/code_calls) needs the optional ``ast-grep`` binary
for JS/TS/Go/Rust. Without it the extractors return [] silently, so every other
doctor check stays green while code_graph_* tools and PPR ranking are no-ops.
This probe surfaces that gap. It must never fail the gate.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from ai_core.doctor import check_codegraph_coverage


def _make_index(
    root: Path,
    files: list[tuple[str, str]],
    symbol_langs: list[str],
    call_langs: list[str] | None = None,
) -> None:
    db = root / ".ai" / "cache" / "code.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    try:
        conn.execute("create table chunks(path text, kind text)")
        conn.execute("create table code_symbols(path text, qualname text, lang text)")
        conn.execute("create table code_calls(path text, callee text, lang text)")
        for rel, kind in files:
            conn.execute("insert into chunks(path, kind) values (?, ?)", (rel, kind))
        for lang in symbol_langs:
            conn.execute(
                "insert into code_symbols(path, qualname, lang) values (?, ?, ?)",
                (f"x.{lang}", "f", lang),
            )
        for lang in call_langs or []:
            conn.execute(
                "insert into code_calls(path, callee, lang) values (?, ?, ?)",
                (f"x.{lang}", "g", lang),
            )
        conn.commit()
    finally:
        conn.close()


def test_probe_never_fails_without_index(tmp_path: Path) -> None:
    chk = check_codegraph_coverage(tmp_path)
    assert chk.ok is True
    assert "not indexed" in chk.detail


def test_reports_gap_when_astgrep_languages_have_no_symbols(tmp_path: Path) -> None:
    _make_index(
        tmp_path,
        [("src/main.rs", "file"), ("src/a.ts", "file"), ("m.py", "file")],
        symbol_langs=["python"],
    )
    chk = check_codegraph_coverage(tmp_path)
    assert chk.ok is True, "probe must never fail the gate"
    assert "degraded" in chk.detail
    assert "rust:1 files" in chk.detail
    assert "typescript:1 files" in chk.detail
    assert "python" not in chk.detail.split("degraded")[1].split(";")[0]


def test_reports_ok_when_symbols_exist_for_every_astgrep_language(tmp_path: Path) -> None:
    _make_index(
        tmp_path,
        [("src/main.rs", "file"), ("src/a.ts", "file")],
        symbol_langs=["rust", "typescript"],
    )
    chk = check_codegraph_coverage(tmp_path)
    assert chk.ok is True
    assert chk.detail.startswith("ok covered=")
    assert "rust=1" in chk.detail


def test_python_only_workspace_is_ok(tmp_path: Path) -> None:
    _make_index(tmp_path, [("m.py", "file")], symbol_langs=["python"])
    chk = check_codegraph_coverage(tmp_path)
    assert chk.ok is True
    assert "python-only" in chk.detail


def test_unreadable_index_does_not_raise(tmp_path: Path) -> None:
    db = tmp_path / ".ai" / "cache" / "code.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_text("not a database")
    chk = check_codegraph_coverage(tmp_path)
    assert chk.ok is True


def test_probe_registered_in_run_checks(tmp_path: Path) -> None:
    from ai_core.doctor import run_checks

    names = {c.name for c in run_checks(tmp_path, lightweight=True, update_scan_state=False)}
    assert "codegraph_coverage" in names


def test_disable_flag_is_reported_distinctly(tmp_path: Path, monkeypatch) -> None:
    """AI_ASTGREP_DISABLE must not be reported as a missing install."""
    monkeypatch.setenv("AI_ASTGREP_DISABLE", "1")
    _make_index(tmp_path, [("src/main.rs", "file")], symbol_langs=["python"])
    chk = check_codegraph_coverage(tmp_path)
    assert chk.ok is True
    assert "AI_ASTGREP_DISABLE" in chk.detail
    assert "brew install" not in chk.detail


def test_newly_supported_languages_are_tracked(tmp_path: Path) -> None:
    """Kotlin and Dart were wired into the indexer; coverage must notice them."""
    _make_index(
        tmp_path,
        [("app/M.kt", "file"), ("lib/ui.dart", "file")],
        symbol_langs=["python"],
    )
    chk = check_codegraph_coverage(tmp_path)
    assert "kotlin:1 files" in chk.detail
    assert "dart:1 files" in chk.detail


def test_call_edges_alone_count_as_coverage(tmp_path: Path) -> None:
    """Real consumer files (an eslint config of object literals, a one-IIFE
    tracking script) declare no NAMED function yet do contain calls. Call edges
    prove extraction ran, so the probe must not report a defect."""
    _make_index(
        tmp_path,
        [("eslint.config.js", "file")],
        symbol_langs=["python"],
        call_langs=["javascript"],
    )
    chk = check_codegraph_coverage(tmp_path)
    assert chk.ok is True
    assert "degraded" not in chk.detail, chk.detail


def test_no_symbols_and_no_calls_is_still_reported(tmp_path: Path) -> None:
    """A genuinely unextracted language must still surface."""
    _make_index(
        tmp_path,
        [("src/main.rs", "file")],
        symbol_langs=["python"],
        call_langs=["python"],
    )
    chk = check_codegraph_coverage(tmp_path)
    assert chk.ok is True
    assert "rust:1 files" in chk.detail
