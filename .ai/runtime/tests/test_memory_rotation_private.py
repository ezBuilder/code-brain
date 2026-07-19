from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ai_core.memory import append_jsonl, rotate_jsonl_tail


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_rotation_refuses_external_symlink_without_truncating_target(tmp_path: Path) -> None:
    external = tmp_path / "external.jsonl"
    external.write_text("".join(json.dumps({"id": index}) + "\n" for index in range(20)), encoding="utf-8")
    path = tmp_path / ".ai" / "memory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    path.symlink_to(external)
    original = external.read_text(encoding="utf-8")

    result = rotate_jsonl_tail(path, max_bytes=20, keep_lines=2)

    assert result["ok"] is False
    assert external.read_text(encoding="utf-8") == original


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_rotation_refuses_external_hardlink_without_truncating_target(tmp_path: Path) -> None:
    external = tmp_path / "external.jsonl"
    external.write_text("".join(json.dumps({"id": index}) + "\n" for index in range(20)), encoding="utf-8")
    path = tmp_path / ".ai" / "memory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    os.link(external, path)
    original = external.read_text(encoding="utf-8")

    result = rotate_jsonl_tail(path, max_bytes=20, keep_lines=2)

    assert result["ok"] is False
    assert external.read_text(encoding="utf-8") == original


def test_concurrent_append_and_rotation_preserve_all_new_records(tmp_path: Path) -> None:
    path = tmp_path / ".ai" / "memory" / "events.jsonl"
    for index in range(100):
        append_jsonl(path, {"id": f"old-{index}", "payload": "x" * 20})

    def append(index: int) -> None:
        append_jsonl(path, {"id": f"new-{index}", "payload": "y" * 20})

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(append, index) for index in range(30)]
        futures.extend(
            pool.submit(rotate_jsonl_tail, path, max_bytes=100_000, keep_lines=500)
            for _ in range(5)
        )
        for future in futures:
            future.result()

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    ids = {row["id"] for row in rows}
    assert {f"new-{index}" for index in range(30)}.issubset(ids)


def test_rotation_dry_run_leaves_file_unchanged(tmp_path: Path) -> None:
    path = tmp_path / ".ai" / "memory" / "events.jsonl"
    for index in range(20):
        append_jsonl(path, {"id": index, "payload": "x" * 20})
    original = path.read_bytes()

    result = rotate_jsonl_tail(path, max_bytes=50, keep_lines=2, dry_run=True)

    assert result["ok"] is True
    assert result["rotated"] is True
    assert path.read_bytes() == original