from __future__ import annotations

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ai_core.memory import append_jsonl, jsonl_lock_path, state_root_for_path


def test_append_jsonl_creates_private_file_and_lock(tmp_path: Path) -> None:
    path = tmp_path / ".ai" / "memory" / "records.jsonl"

    append_jsonl(path, {"id": 1})

    assert json.loads(path.read_text(encoding="utf-8")) == {"id": 1}
    assert state_root_for_path(path) == tmp_path
    assert jsonl_lock_path(path).is_file()
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(jsonl_lock_path(path).stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_append_jsonl_refuses_external_symlink(tmp_path: Path) -> None:
    path = tmp_path / ".ai" / "memory" / "records.jsonl"
    path.parent.mkdir(parents=True)
    external = tmp_path / "external.jsonl"
    external.write_text('{"external":true}\n', encoding="utf-8")
    path.symlink_to(external)

    with pytest.raises(OSError):
        append_jsonl(path, {"id": 1})

    assert external.read_text(encoding="utf-8") == '{"external":true}\n'


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_append_jsonl_refuses_external_hardlink_without_mode_change(tmp_path: Path) -> None:
    path = tmp_path / ".ai" / "memory" / "records.jsonl"
    path.parent.mkdir(parents=True)
    external = tmp_path / "external.jsonl"
    external.write_text('{"external":true}\n', encoding="utf-8")
    original_mode = stat.S_IMODE(external.stat().st_mode)
    os.link(external, path)

    with pytest.raises(OSError, match="hard links"):
        append_jsonl(path, {"id": 1})

    assert external.read_text(encoding="utf-8") == '{"external":true}\n'
    assert stat.S_IMODE(external.stat().st_mode) == original_mode


def test_append_jsonl_concurrent_records_are_complete_and_not_lost(tmp_path: Path) -> None:
    path = tmp_path / ".ai" / "memory" / "records.jsonl"

    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(append_jsonl, path, {"id": index}) for index in range(100)]
        for future in futures:
            future.result()

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 100
    assert {row["id"] for row in rows} == set(range(100))