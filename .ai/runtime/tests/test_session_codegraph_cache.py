from __future__ import annotations

import os
from pathlib import Path

import pytest

from ai_core import codegraph, hooks
from ai_core.search import rebuild


def test_session_codegraph_summary_is_cached(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / ".ai" / "cache" / "code.sqlite"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"index")
    calls = {"count": 0}

    def fake_hotspots(_root: Path, *, limit: int) -> dict:
        calls["count"] += 1
        assert limit == 3
        return {"ok": True, "hotspots": [{"callee": "render", "calls": 7}]}

    monkeypatch.setattr(codegraph, "hotspot_callees", fake_hotspots)

    first = hooks._codegraph_hotspot_context(tmp_path)
    second = hooks._codegraph_hotspot_context(tmp_path)

    assert first == second
    assert "render(7)" in first
    assert calls["count"] == 1


def test_session_codegraph_cache_invalidates_when_index_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / ".ai" / "cache" / "code.sqlite"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"index")
    calls = {"count": 0}

    def fake_hotspots(_root: Path, *, limit: int) -> dict:
        calls["count"] += 1
        return {"ok": True, "hotspots": [{"callee": "query", "calls": calls["count"]}]}

    monkeypatch.setattr(codegraph, "hotspot_callees", fake_hotspots)
    first = hooks._codegraph_hotspot_context(tmp_path)
    cache = tmp_path / ".ai" / "cache" / "codegraph_hotspots.json"
    generation = tmp_path / ".ai" / "cache" / "code-index-generation"
    newer = cache.stat().st_mtime + 2
    os.utime(generation, (newer, newer))
    second = hooks._codegraph_hotspot_context(tmp_path)

    assert first != second
    assert calls["count"] == 2


def test_sqlite_read_mtime_does_not_invalidate_codegraph_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / ".ai" / "cache" / "code.sqlite"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"index")
    calls = {"count": 0}

    def fake_hotspots(_root: Path, *, limit: int) -> dict:
        calls["count"] += 1
        return {"ok": True, "hotspots": [{"callee": "read", "calls": 1}]}

    monkeypatch.setattr(codegraph, "hotspot_callees", fake_hotspots)
    first = hooks._codegraph_hotspot_context(tmp_path)
    cache = tmp_path / ".ai" / "cache" / "codegraph_hotspots.json"
    newer = cache.stat().st_mtime + 2
    os.utime(db, (newer, newer))
    second = hooks._codegraph_hotspot_context(tmp_path)

    assert first == second
    assert calls["count"] == 1


def test_full_rebuild_writes_code_index_generation(tmp_path: Path) -> None:
    source = tmp_path / "src" / "main.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")

    rebuild(tmp_path)

    marker = tmp_path / ".ai" / "cache" / "code-index-generation"
    assert marker.is_file()
    assert marker.read_text(encoding="utf-8").strip().isdigit()


def test_codegraph_context_missing_index_creates_no_database_or_marker(tmp_path: Path) -> None:
    db = tmp_path / ".ai" / "cache" / "code.sqlite"
    marker = tmp_path / ".ai" / "cache" / "code-index-generation"

    assert hooks._codegraph_hotspot_context(tmp_path) == ""
    assert not db.exists()
    assert not marker.exists()


def test_incremental_generation_changes_only_for_real_index_drift(tmp_path: Path) -> None:
    source = tmp_path / "src" / "main.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    rebuild(tmp_path)
    marker = tmp_path / ".ai" / "cache" / "code-index-generation"
    first = marker.read_text(encoding="utf-8")

    rebuild(tmp_path, incremental=True)
    unchanged = marker.read_text(encoding="utf-8")
    source.write_text("VALUE = 2\n", encoding="utf-8")
    rebuild(tmp_path, incremental=True, paths={"src/main.py"})
    changed = marker.read_text(encoding="utf-8")

    assert unchanged == first
    assert changed != first