from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ai_core import process_janitor
from ai_core.federated import _federated_cache_path, cross_project_summary
from ai_core.memory_hot import hot_cache_path, read_hot_cache, write_hot_cache
from ai_core.session_resume import (
    handoff_path,
    prune_snapshots,
    read_handoff,
    read_latest_snapshot,
)


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_process_registry_reader_rejects_external_symlink_without_terminating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    external = tmp_path / "external-registry.jsonl"
    external.write_text(
        json.dumps({"pid": 424242, "created_at": 0, "kind": "injected"}) + "\n",
        encoding="utf-8",
    )
    registry = process_janitor.registry_path(tmp_path)
    registry.parent.mkdir(parents=True)
    registry.symlink_to(external)

    monkeypatch.setattr(
        process_janitor,
        "_terminate",
        lambda _pid: (_ for _ in ()).throw(AssertionError("untrusted registry must not terminate")),
    )

    result = process_janitor.cleanup_children(tmp_path, ttl_seconds=1)

    assert result == {"ok": False, "reason": "registry_unreadable"}
    assert registry.is_symlink()
    assert external.is_file()


@pytest.mark.skipif(os.name == "nt", reason="Unix mode and symlink semantics")
def test_hot_cache_reader_rejects_symlink_and_public_mode(tmp_path: Path) -> None:
    cache = hot_cache_path(tmp_path)
    cache.parent.mkdir(parents=True)
    external = tmp_path / "external-hot.json"
    external.write_text('{"ok":true,"items":[{"ref":"external"}]}', encoding="utf-8")
    cache.symlink_to(external)

    assert read_hot_cache(tmp_path) is None
    assert external.read_text(encoding="utf-8").startswith('{"ok":true')

    cache.unlink()
    write_hot_cache(tmp_path, [{"ref": "local"}], counts={}, limit=1)
    cache.chmod(0o644)
    assert read_hot_cache(tmp_path) is None


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_federated_cache_reader_ignores_external_symlink_and_replaces_link(
    tmp_path: Path,
) -> None:
    self_root = tmp_path / "workspace" / "self"
    manifest = self_root / ".ai" / "generated" / "install-manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    cache = _federated_cache_path(self_root)
    cache.parent.mkdir(parents=True, exist_ok=True)
    external = tmp_path / "external-federated.json"
    external.write_text('{"poisoned":true}', encoding="utf-8")
    cache.symlink_to(external)

    result = cross_project_summary(self_root, home=tmp_path)

    assert result.get("poisoned") is None
    assert not cache.is_symlink()
    assert external.read_text(encoding="utf-8") == '{"poisoned":true}'


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_handoff_reader_rejects_external_symlink(tmp_path: Path) -> None:
    external = tmp_path / "external-handoff.json"
    external.write_text('{"goal":"external"}', encoding="utf-8")
    handoff = handoff_path(tmp_path)
    handoff.parent.mkdir(parents=True)
    handoff.symlink_to(external)

    assert read_handoff(tmp_path) == {}
    assert external.read_text(encoding="utf-8") == '{"goal":"external"}'


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_snapshot_reader_and_pruner_ignore_external_symlinks(tmp_path: Path) -> None:
    session_dir = tmp_path / ".ai" / "memory" / "sessions" / "linked"
    session_dir.mkdir(parents=True)
    external = tmp_path / "external-resume.json"
    external.write_text('{"session_id":"external"}', encoding="utf-8")
    snapshot = session_dir / "resume.json"
    snapshot.symlink_to(external)

    assert read_latest_snapshot(tmp_path) is None
    result = prune_snapshots(tmp_path, older_than_days=0)
    assert result["ok"] is False
    assert result["removed"] == 0
    assert any("unsafe-resume-symlink" in item for item in result["errors"])
    assert snapshot.is_symlink()
    assert external.read_text(encoding="utf-8") == '{"session_id":"external"}'


@pytest.mark.skipif(os.name == "nt", reason="Unix directory symlink semantics")
def test_snapshot_reader_ignores_symlinked_session_directory(tmp_path: Path) -> None:
    base = tmp_path / ".ai" / "memory" / "sessions"
    base.mkdir(parents=True)
    external_dir = tmp_path / "external-session"
    external_dir.mkdir()
    (external_dir / "resume.json").write_text(
        '{"session_id":"external-directory"}',
        encoding="utf-8",
    )
    (base / "linked-session").symlink_to(external_dir, target_is_directory=True)

    assert read_latest_snapshot(tmp_path) is None
