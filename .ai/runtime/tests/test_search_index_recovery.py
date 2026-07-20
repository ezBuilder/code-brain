from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from ai_core import search as search_mod


def _make_repo(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".ai").mkdir()
    (root / ".ai" / "config.yaml").write_text(
        "project_name: index-recovery\n",
        encoding="utf-8",
    )
    source = root / "src" / "main.py"
    source.parent.mkdir(parents=True)
    source.write_text("RecoveredIndexNeedle = True\n", encoding="utf-8")
    return root, source


def _write_corrupt_index(root: Path, *, future: bool = False) -> Path:
    db = search_mod.db_path(root)
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"not-a-sqlite-database")
    if os.name != "nt":
        db.chmod(0o600)
    if future:
        timestamp = time.time() + 86_400
        os.utime(db, (timestamp, timestamp))
    return db


@pytest.mark.parametrize("raises", [False, True])
def test_connection_scope_always_closes(tmp_path: Path, raises: bool) -> None:
    root, _source = _make_repo(tmp_path)
    captured: list[sqlite3.Connection] = []

    if raises:
        with pytest.raises(RuntimeError, match="scope failure"):
            with search_mod._connection_scope(root) as conn:
                captured.append(conn)
                raise RuntimeError("scope failure")
    else:
        with search_mod._connection_scope(root) as conn:
            captured.append(conn)
            conn.execute("create table closed_probe(value integer)")

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        captured[0].execute("select 1")


@pytest.mark.parametrize("incremental", [False, True])
def test_rebuild_recovers_corrupt_disposable_index(
    tmp_path: Path,
    incremental: bool,
) -> None:
    root, _source = _make_repo(tmp_path)
    _write_corrupt_index(root)

    result = search_mod.rebuild(root, incremental=incremental)
    payload = search_mod.query(root, "RecoveredIndexNeedle")

    assert result["ok"] is True
    assert result["recovered_corrupt_index"] is True
    assert any(item["path"] == "src/main.py" for item in payload["results"])


def test_query_recovers_future_dated_corrupt_index(tmp_path: Path) -> None:
    root, _source = _make_repo(tmp_path)
    _write_corrupt_index(root, future=True)

    payload = search_mod.query(root, "RecoveredIndexNeedle")

    assert payload["ok"] is True
    assert payload["auto_refresh"]["rebuilt"] is True
    assert payload["auto_refresh"]["reason"] == "corrupt_index"
    assert any(item["path"] == "src/main.py" for item in payload["results"])


def test_query_degrades_to_fallback_for_noncorrupt_sqlite_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _source = _make_repo(tmp_path)
    fallback_result = {
        "path": "src/main.py",
        "snippet": "L1: RecoveredIndexNeedle = True",
        "line": 1,
        "content": "RecoveredIndexNeedle = True",
        "rank": -0.1,
        "source": "rg",
        "provenance": {
            "processor": "ripgrep-fallback",
            "model_hash": None,
            "prompt_version": None,
            "chunker_version": "rg-1",
            "confidence": 0.5,
        },
    }

    @contextmanager
    def locked_connection(_root: Path):
        raise sqlite3.OperationalError("database is locked")
        yield

    monkeypatch.setattr(search_mod, "_connection_scope", locked_connection)
    monkeypatch.setattr(
        search_mod,
        "_auto_refresh_if_stale",
        lambda _root: {"enabled": True, "rebuilt": False, "reason": "current"},
    )
    monkeypatch.setattr(search_mod, "_rg_fallback", lambda *_args, **_kwargs: [fallback_result])

    payload = search_mod.query(root, "RecoveredIndexNeedle")

    assert payload["ok"] is True
    assert payload["rg_fallback"] is True
    assert payload["auto_refresh"]["reason"] == "index_unavailable"
    assert payload["results"] == [
        {
            "path": fallback_result["path"],
            "snippet": fallback_result["snippet"],
            "provenance": fallback_result["provenance"],
        }
    ]


def test_locked_database_error_is_not_classified_as_corruption() -> None:
    assert search_mod._is_corrupt_index_error(
        sqlite3.OperationalError("database is locked")
    ) is False
