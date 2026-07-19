from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / ".ai" / "runtime" / "src"))

from ai_core import session as session_mod  # noqa: E402
from ai_core.cli import build_parser  # noqa: E402
from ai_core.search import db_path, rebuild  # noqa: E402


def _stub_session_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        session_mod,
        "index_status",
        lambda _root, **_kwargs: {"stale": False, "reason": "current"},
    )
    monkeypatch.setattr(
        session_mod,
        "handle_hook",
        lambda _root, _name, _payload: {
            "ok": True,
            "session_id": "session-test",
            "elapsed_ms": 23,
        },
    )
    monkeypatch.setattr(session_mod, "run_checks", lambda _root, **_kwargs: [])
    monkeypatch.setattr(session_mod, "as_payload", lambda _checks: {"ok": True, "checks": []})

    import ai_core.session_resume as session_resume

    monkeypatch.setattr(
        session_resume,
        "write_snapshot",
        lambda _root, **_kwargs: {"ok": True, "path": ".ai/memory/sessions/session-test/resume.json"},
    )


def test_session_parser_exposes_audit_index_repair() -> None:
    args = build_parser().parse_args(
        ["session", "start", "--repair-audit-index", "--render-manifest"]
    )
    assert args.repair_audit_index is True
    assert args.render_manifest is True


def test_start_session_repairs_audit_index_before_doctor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_session_side_effects(monkeypatch)
    audit_path = tmp_path / ".ai" / "memory" / "audit" / "2026.jsonl"
    audit_path.parent.mkdir(parents=True)
    audit_path.write_text(
        json.dumps(
            {
                "ts": "2026-07-19T00:00:00Z",
                "category": "test",
                "action": "before-session",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    payload = session_mod.start_session(
        tmp_path,
        agent="operator",
        rebuild_mode="never",
        repair_audit_index=True,
    )

    assert payload["audit_index"] == {
        "repaired": True,
        "ok": True,
        "path": ".ai/memory/audit-index.jsonl",
        "indexed": 1,
    }
    index_row = json.loads(
        (tmp_path / ".ai" / "memory" / "audit-index.jsonl").read_text(encoding="utf-8")
    )
    assert index_row["action"] == "before-session"


def test_start_session_dry_run_reports_without_repairing_audit_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_session_side_effects(monkeypatch)

    payload = session_mod.start_session(
        tmp_path,
        agent="operator",
        rebuild_mode="never",
        dry_run=True,
        repair_audit_index=True,
    )

    assert payload["audit_index"] == {
        "repaired": False,
        "would_repair": True,
        "dry_run": True,
    }
    assert not (tmp_path / ".ai" / "memory" / "audit-index.jsonl").exists()


def test_start_session_renders_manifest_in_same_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_session_side_effects(monkeypatch)

    payload = session_mod.start_session(
        tmp_path,
        agent="operator",
        rebuild_mode="never",
        render_manifest=True,
    )

    manifest = tmp_path / ".ai" / "generated" / "manifest.json"
    assert manifest.is_file()
    assert payload["render_manifest"]["planned"][0]["path"] == ".ai/generated/manifest.json"


def test_start_session_dry_run_does_not_write_rendered_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_session_side_effects(monkeypatch)

    payload = session_mod.start_session(
        tmp_path,
        agent="operator",
        rebuild_mode="never",
        dry_run=True,
        render_manifest=True,
    )

    assert payload["render_manifest"]["planned"][0]["changed"] is True
    assert not (tmp_path / ".ai" / "generated" / "manifest.json").exists()


def test_start_session_reuses_precomputed_index_status_for_doctor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = {
        "exists": True,
        "indexed": 3,
        "stale": False,
        "reason": "current",
        "changed_paths": [],
    }
    captured: dict[str, object] = {}
    monkeypatch.setattr(session_mod, "index_status", lambda _root, **_kwargs: status)
    monkeypatch.setattr(
        session_mod,
        "run_checks",
        lambda _root, **kwargs: captured.update(kwargs) or [],
    )
    monkeypatch.setattr(session_mod, "as_payload", lambda _checks: {"ok": True, "checks": []})
    monkeypatch.setattr(
        session_mod,
        "handle_hook",
        lambda _root, _name, _payload: {
            "ok": True,
            "session_id": "session-test",
            "elapsed_ms": 23,
        },
    )
    import ai_core.session_resume as session_resume

    monkeypatch.setattr(
        session_resume,
        "write_snapshot",
        lambda _root, **_kwargs: {"ok": True, "path": "resume.json"},
    )

    session_mod.start_session(tmp_path, agent="operator", rebuild_mode="auto")

    assert captured["precomputed_index_status"] is status
    assert captured["precomputed_session_start_ms"] == 23
    assert captured["lightweight"] is True


def _make_indexed_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    source = repo / "src" / "main.py"
    source.parent.mkdir(parents=True)
    (repo / ".ai").mkdir()
    (repo / ".ai" / "config.yaml").write_text("project_name: session-test\n", encoding="utf-8")
    source.write_text("VALUE = 1\n", encoding="utf-8")
    rebuild(repo)
    return repo, source


def test_index_status_ignores_newer_mtime_when_content_is_unchanged(tmp_path: Path) -> None:
    repo, source = _make_indexed_repo(tmp_path)
    newer = db_path(repo).stat().st_mtime + 5
    os.utime(source, (newer, newer))

    status = session_mod.index_status(repo)

    assert status["stale"] is False
    assert status["reason"] == "current"
    assert status["changed_paths"] == []


def test_index_status_detects_new_and_hash_changed_files(tmp_path: Path) -> None:
    repo, source = _make_indexed_repo(tmp_path)
    source.write_text("VALUE = 2\n", encoding="utf-8")
    old = max(1, db_path(repo).stat().st_mtime - 60)
    os.utime(source, (old, old))
    added = repo / "src" / "added.py"
    added.write_text("ADDED = True\n", encoding="utf-8")

    status = session_mod.index_status(repo)

    assert status["stale"] is True
    assert status["reason"] == "hash_mismatch"
    assert status["changed_paths"] == ["src/added.py", "src/main.py"]


def test_normal_session_uses_metadata_but_strict_session_hashes_all_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[bool, bool]] = []
    status = {
        "exists": True,
        "indexed": 1,
        "stale": False,
        "reason": "current",
        "changed_paths": [],
    }

    def captured_status(
        _root: Path,
        *,
        use_metadata: bool,
        refresh_metadata: bool,
    ):
        calls.append((use_metadata, refresh_metadata))
        return status

    monkeypatch.setattr(session_mod, "index_status", captured_status)
    doctor_modes: list[bool] = []
    monkeypatch.setattr(
        session_mod,
        "run_checks",
        lambda _root, **kwargs: doctor_modes.append(bool(kwargs.get("lightweight"))) or [],
    )
    monkeypatch.setattr(session_mod, "as_payload", lambda _checks: {"ok": True, "checks": []})
    monkeypatch.setattr(
        session_mod,
        "handle_hook",
        lambda _root, _name, _payload: {
            "ok": True,
            "session_id": "session-test",
            "elapsed_ms": 1,
        },
    )
    import ai_core.session_resume as session_resume

    monkeypatch.setattr(
        session_resume,
        "write_snapshot",
        lambda _root, **_kwargs: {"ok": True, "path": "resume.json"},
    )

    session_mod.start_session(tmp_path, agent="operator", rebuild_mode="auto", strict=False)
    session_mod.start_session(tmp_path, agent="operator", rebuild_mode="auto", strict=True)

    assert calls == [(True, True), (False, False)]
    assert doctor_modes == [True, False]


def test_session_passes_scan_state_write_policy_to_doctor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = {
        "exists": True,
        "indexed": 1,
        "stale": False,
        "reason": "current",
        "changed_paths": [],
    }
    captured: list[bool] = []
    monkeypatch.setattr(session_mod, "index_status", lambda _root, **_kwargs: status)
    monkeypatch.setattr(
        session_mod,
        "run_checks",
        lambda _root, **kwargs: captured.append(bool(kwargs["update_scan_state"])) or [],
    )
    monkeypatch.setattr(session_mod, "as_payload", lambda _checks: {"ok": True, "checks": []})
    monkeypatch.setattr(
        session_mod,
        "handle_hook",
        lambda _root, _name, _payload: {
            "ok": True,
            "session_id": "session-test",
            "elapsed_ms": 1,
        },
    )
    import ai_core.session_resume as session_resume

    monkeypatch.setattr(
        session_resume,
        "write_snapshot",
        lambda _root, **_kwargs: {"ok": True, "path": "resume.json"},
    )

    session_mod.start_session(tmp_path, agent="operator", dry_run=False)
    session_mod.start_session(tmp_path, agent="operator", dry_run=True)

    assert captured == [True, False]