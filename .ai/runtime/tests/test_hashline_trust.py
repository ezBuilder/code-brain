from __future__ import annotations

import os
from pathlib import Path

import pytest

from ai_core import hashline, mcp_server


def test_hashline_reads_through_confined_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    source = root / "src" / "sample.txt"
    source.parent.mkdir(parents=True)
    source.write_text("alpha\nbeta\n", encoding="utf-8")
    calls: list[tuple[Path, Path]] = []
    real_read = hashline.read_root_confined_bytes

    def recording_read(path: Path, *, root: Path, **kwargs):
        calls.append((path, root))
        return real_read(path, root=root, **kwargs)

    monkeypatch.setattr(hashline, "read_root_confined_bytes", recording_read)

    payload = hashline.read_hashline(root, "src/sample.txt")

    assert payload["ok"] is True
    assert payload["path"] == "src/sample.txt"
    assert calls == [(source, root)]


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_hashline_rejects_final_symlink_without_external_read(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    external = tmp_path / "outside.txt"
    external.write_text("outside secret\n", encoding="utf-8")
    source = root / "src" / "sample.txt"
    source.parent.mkdir(parents=True)
    source.symlink_to(external)

    with pytest.raises(OSError):
        hashline.read_hashline(root, "src/sample.txt")

    assert external.read_text(encoding="utf-8") == "outside secret\n"


@pytest.mark.skipif(os.name == "nt", reason="Unix directory symlink semantics")
def test_hashline_rejects_parent_symlink_without_external_read(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    external = tmp_path / "outside-dir"
    external.mkdir()
    outside = external / "sample.txt"
    outside.write_text("outside secret\n", encoding="utf-8")
    root.mkdir()
    (root / "src").symlink_to(external, target_is_directory=True)

    with pytest.raises(OSError):
        hashline.read_hashline(root, "src/sample.txt")

    assert outside.read_text(encoding="utf-8") == "outside secret\n"


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
def test_hashline_rejects_hardlinked_file(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    external = tmp_path / "outside.txt"
    external.write_text("outside secret\n", encoding="utf-8")
    source = root / "src" / "sample.txt"
    source.parent.mkdir(parents=True)
    os.link(external, source)

    with pytest.raises(OSError):
        hashline.read_hashline(root, "src/sample.txt")

    assert external.read_text(encoding="utf-8") == "outside secret\n"


def test_hashline_rejects_group_writable_source(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("Unix permission semantics")
    root = tmp_path / "repo"
    source = root / "src" / "sample.txt"
    source.parent.mkdir(parents=True)
    source.write_text("untrusted\n", encoding="utf-8")
    source.chmod(0o666)

    with pytest.raises(PermissionError):
        hashline.read_hashline(root, "src/sample.txt")


def test_hashline_range_is_capped_and_reports_truncation(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    source = root / "sample.txt"
    source.parent.mkdir(parents=True)
    source.write_text("".join(f"line-{index}\n" for index in range(20)), encoding="utf-8")
    original_cap = hashline.MAX_RANGE_LINES
    hashline.MAX_RANGE_LINES = 3
    try:
        payload = hashline.read_hashline(root, "sample.txt", start=5, end=20)
    finally:
        hashline.MAX_RANGE_LINES = original_cap

    assert payload["start"] == 5
    assert payload["end"] == 7
    assert payload["line_count"] == 3
    assert payload["truncated"] is True
    assert [line.split("|", 1)[1] for line in payload["content"].splitlines()] == [
        "line-4",
        "line-5",
        "line-6",
    ]


def test_hashline_default_range_is_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "repo"
    source = root / "sample.txt"
    source.parent.mkdir(parents=True)
    source.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    monkeypatch.setattr(hashline, "MAX_RANGE_LINES", 2)

    payload = hashline.read_hashline(root, "sample.txt")

    assert payload["line_count"] == 2
    assert payload["end"] == 2
    assert payload["truncated"] is True


def test_hashline_max_bytes_cannot_be_increased_by_caller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    source = root / "sample.txt"
    source.parent.mkdir(parents=True)
    source.write_text("x" * 1024, encoding="utf-8")
    monkeypatch.setattr(hashline, "MAX_READ_BYTES", 128)

    with pytest.raises(OSError):
        hashline.read_hashline(root, "sample.txt", max_bytes=10**100)


def test_verify_anchors_rejects_malformed_and_oversized_inputs(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    source = root / "sample.txt"
    source.parent.mkdir(parents=True)
    source.write_text("alpha\n", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid anchor list"):
        hashline.verify_anchors(root, "sample.txt", [{}] * (hashline.MAX_ANCHORS + 1))
    with pytest.raises(ValueError, match="invalid anchor hash"):
        hashline.verify_anchors(root, "sample.txt", [{"line": 1, "hash": "not-a-hash"}])
    valid_hash = hashline.line_hash(1, "alpha")
    with pytest.raises(ValueError, match="content too long"):
        hashline.verify_anchors(
            root,
            "sample.txt",
            [{"line": 1, "hash": valid_hash, "content": "x" * (hashline.MAX_ANCHOR_CONTENT_CHARS + 1)}],
        )


def test_hashline_mcp_schema_publishes_bounds() -> None:
    tool = next(tool for tool in mcp_server.TOOLS if tool["name"] == "code_read_hashline")
    props = tool["inputSchema"]["properties"]

    assert props["path"]["maxLength"] == hashline.MAX_PATH_CHARS
    assert props["start"]["minimum"] == 1
    assert props["end"]["maximum"] == hashline.MAX_LINE_NUMBER
