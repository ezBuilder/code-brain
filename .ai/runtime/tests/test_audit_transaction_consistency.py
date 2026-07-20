from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event

from ai_core import audit_fold, memory


def _record(action: str, *, days_old: int) -> str:
    return json.dumps(
        {
            "ts": (datetime.now(timezone.utc) - timedelta(days=days_old))
            .isoformat()
            .replace("+00:00", "Z"),
            "action": action,
            "category": "test",
            "payload": {},
            "prev_sha": None,
        },
        separators=(",", ":"),
    ) + "\n"


def _index_actions(root: Path) -> list[str]:
    index = root / ".ai" / "memory" / "audit-index.jsonl"
    return [
        str(json.loads(line).get("action"))
        for line in index.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_rebuild_and_append_are_serialized_without_duplicate_index_rows(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "repo"
    memory.append_audit(root, action="first.action", category="test", payload={})

    read_started = Event()
    allow_rebuild = Event()
    real_read = memory.read_root_confined_text

    def paused_read(path: Path, **kwargs):
        result = real_read(path, **kwargs)
        if path.parent.name == "audit":
            read_started.set()
            assert allow_rebuild.wait(timeout=5)
        return result

    monkeypatch.setattr(memory, "read_root_confined_text", paused_read)

    with ThreadPoolExecutor(max_workers=2) as pool:
        rebuild_future = pool.submit(memory.rebuild_audit_index, root)
        assert read_started.wait(timeout=5)
        append_future = pool.submit(
            memory.append_audit,
            root,
            action="second.action",
            category="test",
            payload={},
        )
        time.sleep(0.05)
        assert append_future.done() is False
        allow_rebuild.set()
        assert rebuild_future.result(timeout=5)["ok"] is True
        append_future.result(timeout=5)

    actions = _index_actions(root)
    assert actions.count("first.action") == 1
    assert actions.count("second.action") == 1


def test_fold_rebuilds_derived_index_to_match_rewritten_audit(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    audit = memory.audit_path(root)
    audit.parent.mkdir(parents=True)
    audit.write_text(
        _record("old.action", days_old=90) + _record("recent.action", days_old=0),
        encoding="utf-8",
    )
    assert memory.rebuild_audit_index(root)["indexed"] == 2

    result = audit_fold.fold_old_entries(root, days=30)

    assert result["ok"] is True
    audit_actions = [
        str(json.loads(line).get("action"))
        for line in audit.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    index_actions = _index_actions(root)
    assert sorted(index_actions) == sorted(audit_actions)
    assert "old.action" not in index_actions
    assert "recent.action" in index_actions
    assert "_folded" in index_actions


def test_fold_and_append_leave_index_exactly_consistent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "repo"
    audit = memory.audit_path(root)
    audit.parent.mkdir(parents=True)
    audit.write_text(_record("old.action", days_old=90), encoding="utf-8")
    memory.rebuild_audit_index(root)

    read_started = Event()
    allow_fold = Event()
    real_read = audit_fold.read_state_text

    def paused_read(path: Path, *, max_bytes: int) -> str:
        text = real_read(path, max_bytes=max_bytes)
        read_started.set()
        assert allow_fold.wait(timeout=5)
        return text

    monkeypatch.setattr(audit_fold, "read_state_text", paused_read)

    with ThreadPoolExecutor(max_workers=2) as pool:
        fold_future = pool.submit(audit_fold.fold_old_entries, root, days=30)
        assert read_started.wait(timeout=5)
        append_future = pool.submit(
            memory.append_audit,
            root,
            action="concurrent.action",
            category="test",
            payload={},
        )
        time.sleep(0.05)
        assert append_future.done() is False
        allow_fold.set()
        assert fold_future.result(timeout=5)["ok"] is True
        append_future.result(timeout=5)

    audit_actions = [
        str(json.loads(line).get("action"))
        for line in audit.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert sorted(_index_actions(root)) == sorted(audit_actions)
    assert audit_actions.count("_folded") == 1
    assert audit_actions.count("concurrent.action") == 1
