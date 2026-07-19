from __future__ import annotations

import os
import stat
from pathlib import Path

from ai_core.federated import _write_federated_cache
from ai_core.memory_hot import hot_cache_path, write_hot_cache
from ai_core.process_janitor import register_child, registry_path
from ai_core.session_resume import handoff_path, write_handoff, write_snapshot


def _assert_private(path: Path) -> None:
    assert path.is_file()
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_sensitive_runtime_writers_create_private_files(tmp_path: Path) -> None:
    federated = tmp_path / ".ai" / "cache" / "federated_hot.json"
    _write_federated_cache(federated, {"ok": True}, root=tmp_path)
    write_hot_cache(tmp_path, [], counts={}, limit=0)
    register_child(tmp_path, pid=12345, kind="test", command=["python", "worker.py"])
    write_handoff(tmp_path, goal="continue", agent="operator")
    write_snapshot(tmp_path, session_id="private-session", agent="operator")

    for path in (
        federated,
        hot_cache_path(tmp_path),
        registry_path(tmp_path),
        handoff_path(tmp_path),
        tmp_path / ".ai" / "memory" / "sessions" / "private-session" / "resume.json",
    ):
        _assert_private(path)