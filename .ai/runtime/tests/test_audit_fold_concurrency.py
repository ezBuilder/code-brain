from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event

from ai_core import audit_fold, memory


def _old_record(action: str) -> str:
    return json.dumps(
        {
            "ts": (datetime.now(timezone.utc) - timedelta(days=90))
            .isoformat()
            .replace("+00:00", "Z"),
            "action": action,
            "category": "test",
            "payload": {},
        },
        separators=(",", ":"),
    ) + "\n"


def test_fold_holds_append_lock_across_read_modify_write(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "repo"
    audit_file = memory.audit_path(root)
    audit_file.parent.mkdir(parents=True)
    audit_file.write_text(_old_record("old.action"), encoding="utf-8")

    read_completed = Event()
    allow_fold_write = Event()
    real_read = audit_fold.read_state_text

    def paused_read(path: Path, *, max_bytes: int) -> str:
        text = real_read(path, max_bytes=max_bytes)
        read_completed.set()
        assert allow_fold_write.wait(timeout=5)
        return text

    monkeypatch.setattr(audit_fold, "read_state_text", paused_read)

    with ThreadPoolExecutor(max_workers=2) as pool:
        fold_future = pool.submit(audit_fold.fold_old_entries, root, days=30)
        assert read_completed.wait(timeout=5)
        append_future = pool.submit(
            memory.append_audit,
            root,
            action="concurrent.action",
            category="test",
            payload={"sequence": 1},
        )
        time.sleep(0.05)
        assert append_future.done() is False
        allow_fold_write.set()
        fold_result = fold_future.result(timeout=5)
        append_future.result(timeout=5)

    records = [
        json.loads(line)
        for line in audit_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    actions = [record.get("action") for record in records]
    assert fold_result["ok"] is True
    assert actions.count("_folded") == 1
    assert actions.count("concurrent.action") == 1


def test_dry_run_holds_lock_until_snapshot_is_complete(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "repo"
    audit_file = memory.audit_path(root)
    audit_file.parent.mkdir(parents=True)
    original = _old_record("old.action")
    audit_file.write_text(original, encoding="utf-8")

    read_completed = Event()
    allow_finish = Event()
    real_read = audit_fold.read_state_text

    def paused_read(path: Path, *, max_bytes: int) -> str:
        text = real_read(path, max_bytes=max_bytes)
        read_completed.set()
        assert allow_finish.wait(timeout=5)
        return text

    monkeypatch.setattr(audit_fold, "read_state_text", paused_read)

    with ThreadPoolExecutor(max_workers=2) as pool:
        fold_future = pool.submit(audit_fold.fold_old_entries, root, days=30, dry_run=True)
        assert read_completed.wait(timeout=5)
        append_future = pool.submit(
            memory.append_audit,
            root,
            action="concurrent.action",
            category="test",
            payload={},
        )
        time.sleep(0.05)
        assert append_future.done() is False
        allow_finish.set()
        result = fold_future.result(timeout=5)
        append_future.result(timeout=5)

    assert result["ok"] is True
    assert result["dry_run"] is True
    content = audit_file.read_text(encoding="utf-8")
    assert original.strip() in content
    assert '"action":"concurrent.action"' in content


def test_non_mapping_json_is_preserved_during_fold(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    audit_file = memory.audit_path(root)
    audit_file.parent.mkdir(parents=True)
    audit_file.write_text(
        _old_record("old.action") + '["unexpected", "shape"]\n',
        encoding="utf-8",
    )

    result = audit_fold.fold_old_entries(root, days=30)

    assert result["ok"] is True
    content = audit_file.read_text(encoding="utf-8")
    assert '["unexpected", "shape"]' in content
    assert '"action":"_folded"' in content
