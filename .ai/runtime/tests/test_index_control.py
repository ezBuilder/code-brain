from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from ai_core import doctor, index_control, search, session


def _repo(tmp_path: Path, *, indexing: str = "") -> Path:
    root = tmp_path / "repo"
    (root / ".ai").mkdir(parents=True)
    config = "version: 1\nproject_name: index-control-test\nsearch:\n  retriever: bm25\n"
    if indexing:
        config += "  indexing:\n" + "".join(f"    {line}\n" for line in indexing.splitlines())
    (root / ".ai" / "config.yaml").write_text(config, encoding="utf-8")
    return root


def _source(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_policy_config_and_environment_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(
        tmp_path,
        indexing="enabled: false\nauto_rebuild: false\nmax_files: 12\nmax_candidates: 20\nmax_seconds: 9",
    )
    monkeypatch.setenv("AI_INDEX_ENABLED", "true")
    monkeypatch.setenv("AI_INDEX_MAX_FILES", "7")

    payload = index_control.policy(root, max_seconds=3)

    assert payload["ok"] is True
    assert payload["enabled"] is True
    assert payload["auto_rebuild"] is False
    assert payload["max_files"] == 7
    assert payload["max_candidates"] == 20
    assert payload["max_seconds"] == 3
    assert payload["sources"]["enabled"] == "AI_INDEX_ENABLED"
    assert payload["sources"]["max_seconds"] == "argument"


def test_missing_config_uses_safe_defaults(tmp_path: Path) -> None:
    root = tmp_path / "plain-repo"
    root.mkdir()

    payload = index_control.policy(root)

    assert payload["ok"] is True
    assert payload["enabled"] is True
    assert payload["auto_rebuild"] is True
    assert payload["errors"] == []


def test_invalid_policy_fails_closed(tmp_path: Path) -> None:
    root = _repo(tmp_path, indexing="enabled: maybe\nmax_files: 0\nmax_candidates: 1")

    payload = index_control.policy(root)

    assert payload["ok"] is False
    assert any("boolean" in item for item in payload["errors"])
    assert any("max_files" in item for item in payload["errors"])


def test_disabled_rebuild_requires_explicit_force(tmp_path: Path) -> None:
    root = _repo(tmp_path, indexing="enabled: false\nauto_rebuild: false")
    _source(root, "src/main.py", "DISABLED_INDEX_NEEDLE = True\n")

    denied = search.rebuild(root)

    assert denied["ok"] is False
    assert denied["error"] == "INDEXING_DISABLED"
    assert denied["committed"] is False
    assert not search.db_path(root).exists()

    forced = search.rebuild(root, force=True)

    assert forced["ok"] is True
    assert forced["committed"] is True
    status = search.index_control_status(root)
    assert status["ok"] is True
    assert status["policy"]["enabled"] is False
    assert status["progress"]["state"] == "complete"
    assert status["progress"]["suppressed_by_policy"] is True


def test_rebuild_limit_rolls_back_existing_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    source = _source(root, "src/main.py", "OLD_INDEX_NEEDLE = True\n")
    baseline = search.rebuild(root)
    assert baseline["ok"] is True
    with sqlite3.connect(search.db_path(root)) as conn:
        before = conn.execute("select path, sha256 from chunks where path = 'src/main.py'").fetchone()
    assert before is not None

    source.write_text("NEW_INDEX_NEEDLE = True\n", encoding="utf-8")
    _source(root, "src/second.py", "SECOND_INDEX_NEEDLE = True\n")
    monkeypatch.setenv("AI_INDEX_MAX_FILES", "1")

    limited = search.rebuild(root)

    assert limited["ok"] is False
    assert limited["error"] == "INDEX_SCAN_LIMIT"
    assert limited["limit"]["name"] == "max_files"
    assert limited["committed"] is False
    with sqlite3.connect(search.db_path(root)) as conn:
        after = conn.execute("select path, sha256 from chunks where path = 'src/main.py'").fetchone()
        second = conn.execute("select count(*) from chunks where path = 'src/second.py'").fetchone()[0]
    assert after == before
    assert second == 0
    progress = index_control.progress_status(root)
    assert progress["state"] == "failed"
    assert progress["reason"] == "INDEX_SCAN_LIMIT"
    assert progress["limit"]["name"] == "max_files"


def test_stalled_orphaned_progress_is_diagnostic_and_disable_suppresses_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    now = __import__("time").time()
    index_control.write_progress(
        root,
        {
            "schema_version": index_control.PROGRESS_SCHEMA_VERSION,
            "state": "running",
            "operation": "full",
            "phase": "indexing",
            "pid": 99_999_999,
            "started_at_unix": now - 60,
            "updated_at_unix": now - 60,
            "finished_at_unix": None,
            "elapsed_ms": 60_000,
            "scanned_files": 10,
            "indexed_files": 9,
            "source_bytes": 100,
            "candidate_files": 10,
            "candidate_bytes": 100,
            "current_path": "src/main.py",
            "complete": False,
            "partial": False,
            "committed": False,
            "policy": {},
        },
    )
    monkeypatch.setenv("AI_INDEX_STALL_SECONDS", "1")

    unhealthy = index_control.progress_status(root)

    assert unhealthy["ok"] is False
    assert unhealthy["stalled"] is True
    assert unhealthy["orphaned"] is True

    monkeypatch.setenv("AI_INDEX_ENABLED", "0")
    disabled = index_control.progress_status(root)

    assert disabled["ok"] is True
    assert disabled["suppressed_by_policy"] is True
    check = doctor.check_index_control(root)
    assert check.ok is True
    assert "historical-suppressed" in check.detail


def test_disabled_freshness_scan_does_not_create_or_enumerate_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path, indexing="enabled: false")

    def unexpected_candidates(*_args, **_kwargs):
        raise AssertionError("disabled indexing must not enumerate candidates")

    monkeypatch.setattr(search, "candidate_files", unexpected_candidates)
    payload = search.index_hash_status(root)

    assert payload["ok"] is True
    assert payload["reason"] == "indexing_disabled"
    assert not search.db_path(root).exists()


def test_disabled_query_does_not_create_empty_sqlite_index(tmp_path: Path) -> None:
    root = _repo(tmp_path, indexing="enabled: false")
    _source(root, "src/main.py", "NO_INDEX_QUERY_NEEDLE = True\n")

    payload = search.query(root, "NO_INDEX_QUERY_NEEDLE")

    assert payload["ok"] is True
    assert payload["reason"] == "indexing_disabled_no_existing_index"
    assert payload["retrieval_policy"] == "disabled-no-index"
    assert payload["results"] == []
    assert not search.db_path(root).exists()


def test_freshness_catalog_is_bounded_before_source_enumeration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    _source(root, "src/a.py", "A = True\n")
    _source(root, "src/b.py", "B = True\n")
    assert search.rebuild(root)["ok"] is True
    monkeypatch.setenv("AI_INDEX_MAX_FILES", "1")

    def unexpected_candidates(*_args, **_kwargs):
        raise AssertionError("catalog limit must fail before source enumeration")

    monkeypatch.setattr(search, "candidate_files", unexpected_candidates)
    payload = search.index_hash_status(root)

    assert payload["ok"] is False
    assert payload["reason"] == "index_scan_limit"
    assert payload["limit"]["name"] == "max_files"


def test_bounded_candidate_discovery_never_uses_unbounded_cache_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    source = _source(root, "src/main.py", "VALUE = True\n")
    effective = index_control.policy(root)
    progress = index_control.IndexProgress(
        root=root,
        operation="test",
        effective_policy=effective,
        persist=False,
    )
    progress.begin()

    def unexpected_cache(_root: Path):
        raise AssertionError("bounded operations must bypass candidate cache validation")

    monkeypatch.setattr(search, "_candidate_cache_load", unexpected_cache)
    paths = search.candidate_files(root, use_cache=True, update_cache=True, progress=progress)

    assert source in paths


def test_session_auto_rebuild_honors_disabled_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path, indexing="enabled: false\nauto_rebuild: true")
    monkeypatch.setattr(
        session,
        "handle_hook",
        lambda *_args, **_kwargs: {"ok": True, "session_id": "s1", "elapsed_ms": 1},
    )
    monkeypatch.setattr(session, "run_checks", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(session, "as_payload", lambda _checks: {"ok": True, "checks": []})
    import ai_core.session_resume as session_resume

    monkeypatch.setattr(
        session_resume,
        "write_snapshot",
        lambda *_args, **_kwargs: {"ok": True, "path": "resume.json"},
    )

    payload = session.start_session(root, agent="operator", rebuild_mode="always")

    assert payload["index"]["rebuilt"] is False
    assert payload["index"]["skipped"] == "indexing_disabled"
    assert payload["index"]["would_rebuild"] is False
    assert not search.db_path(root).exists()


def test_session_surfaces_attempted_rebuild_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repo(tmp_path)
    status = {
        "exists": True,
        "indexed": 1,
        "stale": True,
        "reason": "hash_mismatch",
        "changed_paths": ["src/main.py"],
        "policy": index_control.policy(root),
    }
    monkeypatch.setattr(session, "index_status", lambda *_args, **_kwargs: status)
    monkeypatch.setattr(
        session,
        "rebuild",
        lambda *_args, **_kwargs: {"ok": False, "error": "INDEX_SCAN_LIMIT", "committed": False},
    )
    monkeypatch.setattr(
        session,
        "handle_hook",
        lambda *_args, **_kwargs: {"ok": True, "session_id": "s1", "elapsed_ms": 1},
    )
    monkeypatch.setattr(session, "run_checks", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(session, "as_payload", lambda _checks: {"ok": True, "checks": []})
    import ai_core.session_resume as session_resume

    monkeypatch.setattr(
        session_resume,
        "write_snapshot",
        lambda *_args, **_kwargs: {"ok": True, "path": "resume.json"},
    )

    payload = session.start_session(root, agent="operator", rebuild_mode="auto")

    assert payload["ok"] is False
    assert payload["index"]["rebuilt"] is False
    assert payload["index"]["result"]["error"] == "INDEX_SCAN_LIMIT"


def test_progress_file_is_private_and_bounded(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    progress = index_control.IndexProgress(
        root=root,
        operation="full",
        effective_policy=index_control.policy(root),
    )
    progress.begin()
    path = index_control.progress_path(root)

    assert path.is_file()
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["state"] == "running"
    assert path.stat().st_size <= index_control.PROGRESS_MAX_BYTES
