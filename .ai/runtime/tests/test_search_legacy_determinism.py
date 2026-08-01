"""-012 (option A): structural legacy indexes answer deterministically.

A pre-v2 content-schema index used to fork on file mtimes: a stale-looking
worktree sent auto-refresh into rebuild() — which migrates legacy — while a
fresh-looking one raised the explicit-rebuild error. Same repo, same query,
two answers. Option A closes the migration side door: read paths NEVER touch
a structural legacy index; only explicit `ai index rebuild` migrates.

Also guarded here, per the hardening bar for this round:
  - steady-state storage growth from repeated failing legacy queries is ZERO
    (no per-query logs/audit/locks/sidecars),
  - no rebuild-lock residue on the skip path,
  - the fix adds no probe work: fresh-path queries still run zero hash-status
    probes, the legacy dirty branch stops after a single probe.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core import search as search_mod  # noqa: E402
from ai_core.search import (  # noqa: E402
    MTIME_STALE_GRACE_SECONDS,
    SCHEMA_VERSION,
    connect,
    query,
    rebuild,
)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".ai").mkdir(parents=True)
    (repo / ".ai" / "config.yaml").write_text("project_name: t\n", encoding="utf-8")
    return repo


def _write_legacy_db(repo: Path) -> Path:
    db = repo / ".ai" / "cache" / "code.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            create table chunks (
              id integer primary key,
              path text not null,
              sha256 text not null,
              content text not null,
              updated_at text default current_timestamp
            );
            create virtual table chunks_fts using fts5(path, content, content='chunks', content_rowid='id');
            """
        )
    return db


def _shift_mtime(path: Path, delta_seconds: float) -> None:
    state = path.stat()
    target = state.st_mtime + delta_seconds
    os.utime(path, (target, target))


def _ai_snapshot(repo: Path) -> list[tuple[str, int]]:
    """Deterministic (relpath, size) listing of everything under .ai."""
    out: list[tuple[str, int]] = []
    for path in sorted((repo / ".ai").rglob("*")):
        if path.is_file():
            out.append((path.relative_to(repo).as_posix(), path.stat().st_size))
    return out


@pytest.fixture(autouse=True)
def _auto_refresh_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("AI_SEARCH_AUTO_REFRESH", raising=False)
    yield


def test_legacy_query_fails_identically_whether_index_looks_fresh_or_stale(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("def anything(): pass\n", encoding="utf-8")
    db = _write_legacy_db(repo)

    # Arm 1 — index looks FRESH (db newer than every source by more than the
    # grace window): the mtime gate skips hash probing entirely.
    _shift_mtime(db, MTIME_STALE_GRACE_SECONDS + 120)
    with pytest.raises(RuntimeError, match="legacy search index schema") as fresh_exc:
        query(repo, "anything", limit=3)

    # Arm 2 — index looks STALE (source newer than db): pre-fix this took the
    # hash-probe branch, got reason=legacy_schema, and REBUILT (= migrated).
    _shift_mtime(repo / "src" / "app.py", MTIME_STALE_GRACE_SECONDS + 240)
    before_bytes = db.read_bytes()
    status = search_mod._auto_refresh_if_stale(repo)
    assert status == {
        "enabled": True,
        "rebuilt": False,
        "reason": "legacy_requires_explicit_rebuild",
    }
    with pytest.raises(RuntimeError, match="legacy search index schema") as stale_exc:
        query(repo, "anything", limit=3)

    assert str(fresh_exc.value) == str(stale_exc.value), "one query, one answer"
    assert db.read_bytes() == before_bytes, "read path must not touch the index"
    with sqlite3.connect(db) as conn:
        columns = [row[1] for row in conn.execute("pragma table_info(chunks)").fetchall()]
    assert "content" in columns, "legacy schema must survive read attempts untouched"


def test_legacy_dirty_branch_skips_after_a_single_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path)
    source = repo / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("def anything(): pass\n", encoding="utf-8")
    git = ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git[:3], "init", "-q"], check=True)
    subprocess.run([*git[:3], "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "seed"], check=True)
    source.write_text("def anything(): return 1\n", encoding="utf-8")  # tracked drift
    _write_legacy_db(repo)

    probes = {"count": 0}
    real_status = search_mod.index_hash_status

    def counting_status(*args, **kwargs):
        probes["count"] += 1
        return real_status(*args, **kwargs)

    monkeypatch.setattr(search_mod, "index_hash_status", counting_status)

    def forbidden_rebuild(*_args, **_kwargs):
        raise AssertionError("read path must never rebuild a structural legacy index")

    monkeypatch.setattr(search_mod, "rebuild", forbidden_rebuild)

    status = search_mod._auto_refresh_if_stale(repo)

    assert status["reason"] == "legacy_requires_explicit_rebuild"
    assert probes["count"] == 1, "legacy verdict must not trigger a second index open"


def test_explicit_rebuild_is_the_one_migration_door(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("def anything(): pass\n", encoding="utf-8")
    db = _write_legacy_db(repo)

    result = rebuild(repo)
    assert result.get("ok") is True

    payload = query(repo, "anything", limit=3)
    assert payload["ok"] is True
    assert any(item["path"].startswith("src/app.py") for item in payload["results"])
    with connect(repo) as conn:
        version = int(conn.execute("pragma user_version").fetchone()[0])
        columns = [row["name"] for row in conn.execute("pragma table_info(chunks)").fetchall()]
    assert version == SCHEMA_VERSION
    assert "content" not in columns
    assert db.exists()


def test_repeated_legacy_queries_grow_storage_by_zero_bytes(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("def anything(): pass\n", encoding="utf-8")
    _write_legacy_db(repo)
    # keep the worktree on the WORST arm (stale → probe branch) for every call
    _shift_mtime(repo / "src" / "app.py", MTIME_STALE_GRACE_SECONDS + 240)

    # warm-up: first contact may mint bounded one-time sidecars (WAL/SHM)
    with pytest.raises(RuntimeError):
        query(repo, "anything", limit=3)
    baseline = _ai_snapshot(repo)

    for _ in range(25):
        with pytest.raises(RuntimeError):
            query(repo, "anything", limit=3)

    assert _ai_snapshot(repo) == baseline, (
        "steady-state legacy failures must not append logs, audit rows, locks, "
        "or index bytes"
    )
    assert not (repo / ".ai" / "cache" / ".rebuild.lock").exists()


def test_fresh_path_runs_zero_hash_probes_and_never_rebuilds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("def fetch_flight_board(): pass\n", encoding="utf-8")
    assert rebuild(repo).get("ok") is True
    db = repo / ".ai" / "cache" / "code.sqlite"
    _shift_mtime(db, MTIME_STALE_GRACE_SECONDS + 120)

    probes = {"count": 0}
    real_status = search_mod.index_hash_status

    def counting_status(*args, **kwargs):
        probes["count"] += 1
        return real_status(*args, **kwargs)

    monkeypatch.setattr(search_mod, "index_hash_status", counting_status)
    monkeypatch.setattr(
        search_mod,
        "rebuild",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no rebuild on fresh path")),
    )

    started = time.perf_counter()
    payload = query(repo, "fetch flight board", limit=3)
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    assert payload["ok"] is True
    assert payload["auto_refresh"]["reason"] == "current"
    assert probes["count"] == 0, "-012 fix must add zero probe work to the fresh path"
    assert elapsed_ms < 2000.0
