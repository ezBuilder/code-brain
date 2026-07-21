from __future__ import annotations

import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ai_core import private_write


def test_atomic_private_write_creates_expected_content_and_mode(tmp_path: Path) -> None:
    path = tmp_path / "cache" / "state.json"

    private_write.atomic_write_private_text(path, '{"ok":true}\n')

    assert path.read_text(encoding="utf-8") == '{"ok":true}\n'
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_atomic_private_write_replaces_symlink_without_touching_target(tmp_path: Path) -> None:
    external = tmp_path / "external.txt"
    external.write_text("external\n", encoding="utf-8")
    path = tmp_path / "state.json"
    path.symlink_to(external)

    private_write.atomic_write_private_text(path, "private\n")

    assert not path.is_symlink()
    assert path.read_text(encoding="utf-8") == "private\n"
    assert external.read_text(encoding="utf-8") == "external\n"


def test_atomic_private_write_cleans_temporary_file_on_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "state.json"

    def failed_replace(*_args, **_kwargs):
        raise OSError("replace failed")

    monkeypatch.setattr(private_write.os, "replace", failed_replace)

    with pytest.raises(OSError, match="replace failed"):
        private_write.atomic_write_private_text(path, "private\n")

    assert list(tmp_path.glob(".state.json.*.tmp")) == []


def test_atomic_private_lines_streams_content_and_reports_bytes(tmp_path: Path) -> None:
    root = tmp_path / "project"
    path = root / ".ai" / "cache" / "stream.jsonl"

    written = private_write.atomic_write_private_lines(
        path,
        (f'{{"index":{index}}}\n' for index in range(100)),
        root=root,
        max_bytes=100_000,
    )

    content = path.read_text(encoding="utf-8")
    assert content.startswith('{"index":0}\n')
    assert content.endswith('{"index":99}\n')
    assert written == len(content.encode("utf-8"))
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_atomic_private_lines_size_limit_preserves_existing_file(tmp_path: Path) -> None:
    root = tmp_path / "project"
    path = root / ".ai" / "cache" / "stream.jsonl"
    private_write.atomic_write_private_text(path, "existing\n", root=root)

    with pytest.raises(private_write.PrivateWriteSizeLimit) as error:
        private_write.atomic_write_private_lines(
            path,
            ("x" * 100 + "\n" for _ in range(10)),
            root=root,
            max_bytes=150,
        )

    assert error.value.current > error.value.maximum
    assert path.read_text(encoding="utf-8") == "existing\n"
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []


def test_private_append_creates_private_file_and_preserves_records(tmp_path: Path) -> None:
    path = tmp_path / "registry.jsonl"

    private_write.append_private_text(path, "one\n")
    private_write.append_private_text(path, "two\n")

    assert path.read_text(encoding="utf-8") == "one\ntwo\n"
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_private_append_refuses_symlink_target(tmp_path: Path) -> None:
    external = tmp_path / "external.txt"
    external.write_text("external\n", encoding="utf-8")
    path = tmp_path / "registry.jsonl"
    path.symlink_to(external)

    with pytest.raises(OSError, match="symlink"):
        private_write.append_private_text(path, "private\n")

    assert external.read_text(encoding="utf-8") == "external\n"


def test_private_append_retries_partial_os_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "registry.jsonl"
    real_write = private_write.os.write
    calls = {"count": 0}

    def partial_write(fd: int, data) -> int:
        calls["count"] += 1
        chunk = bytes(data)
        limit = max(1, len(chunk) // 2)
        return real_write(fd, chunk[:limit])

    monkeypatch.setattr(private_write.os, "write", partial_write)

    private_write.append_private_text(path, "complete-record\n")

    assert calls["count"] > 1
    assert path.read_text(encoding="utf-8") == "complete-record\n"


def test_private_append_size_limit_preserves_existing_file(tmp_path: Path) -> None:
    root = tmp_path / "project"
    path = root / ".ai" / "memory" / "state.jsonl"
    private_write.atomic_write_private_text(path, "one\n", root=root)
    maximum = len("one\ntwo\n".encode("utf-8")) - 1

    with pytest.raises(private_write.PrivateWriteSizeLimit) as error:
        private_write.append_private_text(
            path,
            "two\n",
            root=root,
            max_bytes=maximum,
        )

    assert error.value.current == len("one\ntwo\n".encode("utf-8"))
    assert error.value.maximum == maximum
    assert path.read_text(encoding="utf-8") == "one\n"


def test_private_append_exact_size_limit_succeeds(tmp_path: Path) -> None:
    root = tmp_path / "project"
    path = root / ".ai" / "memory" / "state.jsonl"
    private_write.atomic_write_private_text(path, "one\n", root=root)
    expected = len("one\ntwo\n".encode("utf-8"))

    final_size = private_write.append_private_text(
        path,
        "two\n",
        root=root,
        max_bytes=expected,
    )

    assert final_size == expected
    assert path.read_text(encoding="utf-8") == "one\ntwo\n"


def test_private_append_concurrency_never_crosses_size_limit(tmp_path: Path) -> None:
    root = tmp_path / "project"
    path = root / ".ai" / "memory" / "state.jsonl"
    maximum = 1024

    def append(index: int) -> bool:
        try:
            private_write.append_private_text(
                path,
                f"{index:04d}:" + ("x" * 20) + "\n",
                root=root,
                max_bytes=maximum,
            )
            return True
        except private_write.PrivateWriteSizeLimit:
            return False

    with ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = list(pool.map(append, range(200)))

    content = path.read_text(encoding="utf-8")
    assert any(outcomes)
    assert not all(outcomes)
    assert path.stat().st_size <= maximum
    assert all(len(line) == 25 and line[4] == ":" for line in content.splitlines())


@pytest.mark.skipif(os.name == "nt", reason="Unix directory symlink semantics")
@pytest.mark.parametrize("operation", ["replace", "append"])
def test_private_writer_rejects_external_parent_symlink(
    tmp_path: Path,
    operation: str,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (root / ".ai").symlink_to(external, target_is_directory=True)
    path = root / ".ai" / "cache" / "state.json"

    with pytest.raises(OSError, match="escapes project root"):
        if operation == "replace":
            private_write.atomic_write_private_text(path, "private\n", root=root)
        else:
            private_write.append_private_text(path, "private\n", root=root)

    assert not (external / "cache" / "state.json").exists()


def test_root_confined_reader_returns_text_and_stat(tmp_path: Path) -> None:
    root = tmp_path / "project"
    path = root / ".ai" / "cache" / "state.json"
    private_write.atomic_write_private_text(path, "private\n", root=root)

    text, state = private_write.read_root_confined_text(path, root=root)

    assert text == "private\n"
    assert state.st_size == len("private\n")


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink and mode semantics")
def test_root_confined_reader_rejects_symlink_and_public_mode(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    external = tmp_path / "external.txt"
    external.write_text("external\n", encoding="utf-8")
    linked = root / "linked.txt"
    linked.symlink_to(external)

    with pytest.raises(OSError, match="symlink"):
        private_write.read_root_confined_text(linked, root=root)

    public = root / "public.txt"
    public.write_text("public\n", encoding="utf-8")
    public.chmod(0o644)
    with pytest.raises(PermissionError, match="permissions"):
        private_write.read_root_confined_text(public, root=root)

    text, _state = private_write.read_root_confined_text(
        public,
        root=root,
        require_private=False,
    )
    assert text == "public\n"


def test_root_confined_reader_enforces_size_limit(tmp_path: Path) -> None:
    root = tmp_path / "project"
    path = root / ".ai" / "cache" / "large.txt"
    private_write.atomic_write_private_text(path, "x" * 20, root=root)

    with pytest.raises(OSError, match="exceeds"):
        private_write.read_root_confined_text(path, root=root, max_bytes=10)


def test_root_confined_tail_reads_only_complete_suffix_lines(tmp_path: Path) -> None:
    root = tmp_path / "project"
    path = root / ".ai" / "cache" / "large.txt"
    private_write.atomic_write_private_text(
        path,
        ("x" * 1000) + "\none\ntwo\n",
        root=root,
    )

    data, state, truncated = private_write.read_root_confined_tail(
        path,
        root=root,
        max_bytes=32,
    )

    assert data == b"one\ntwo\n"
    assert state.st_size > 32
    assert truncated is True


def test_root_confined_tail_can_open_file_larger_than_read_budget(tmp_path: Path) -> None:
    root = tmp_path / "project"
    path = root / ".ai" / "cache" / "large.txt"
    private_write.atomic_write_private_text(path, ("x" * 10_000) + "\nlast\n", root=root)

    data, _state, truncated = private_write.read_root_confined_tail(
        path,
        root=root,
        max_bytes=16,
    )

    assert data == b"last\n"
    assert truncated is True


def test_private_file_lock_creates_private_lock_file(tmp_path: Path) -> None:
    root = tmp_path / "project"
    path = root / ".ai" / "cache" / ".state.lock"

    with private_write.private_file_lock(path, root=root):
        assert path.is_file()

    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_private_file_lock_refuses_symlink_target(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    external = tmp_path / "external.lock"
    external.write_bytes(b"x")
    path = root / ".lock"
    path.symlink_to(external)

    with pytest.raises(OSError, match="symlink"):
        with private_write.private_file_lock(path, root=root):
            pass

    assert external.read_bytes() == b"x"


def test_private_file_lock_fails_closed_when_locking_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    path = root / ".ai" / "cache" / ".state.lock"

    def unavailable(_handle) -> None:
        raise OSError("exclusive file locking unavailable")

    monkeypatch.setattr(private_write, "_lock_handle_required", unavailable)

    with pytest.raises(OSError, match="locking unavailable"):
        with private_write.private_file_lock(path, root=root):
            raise AssertionError("lock body must not run")


@pytest.mark.skipif(os.name == "nt", reason="Unix directory symlink semantics")
def test_root_confined_directory_rejects_external_parent_symlink(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (root / ".ai").symlink_to(external, target_is_directory=True)

    with pytest.raises(OSError, match="escapes project root"):
        private_write.ensure_root_confined_directory(
            root / ".ai" / "memory" / "sessions" / "safe",
            root=root,
        )

    assert not (external / "memory" / "sessions" / "safe").exists()


@pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
@pytest.mark.parametrize("operation", ["append", "read", "lock"])
def test_private_file_operations_reject_external_hardlink(
    tmp_path: Path,
    operation: str,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    external = tmp_path / "external.txt"
    external.write_text("external\n", encoding="utf-8")
    original_mode = stat.S_IMODE(external.stat().st_mode)
    linked = root / "linked.txt"
    os.link(external, linked)

    with pytest.raises(OSError, match="hard links"):
        if operation == "append":
            private_write.append_private_text(linked, "private\n", root=root)
        elif operation == "read":
            private_write.read_root_confined_text(linked, root=root)
        else:
            with private_write.private_file_lock(linked, root=root):
                pass

    assert external.read_text(encoding="utf-8") == "external\n"
    assert stat.S_IMODE(external.stat().st_mode) == original_mode


@pytest.mark.skipif(os.name == "nt" or not hasattr(os, "geteuid"), reason="POSIX owner check")
@pytest.mark.parametrize("operation", ["append", "lock"])
def test_private_mutation_rejects_effective_owner_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    path = root / "state.txt"
    path.write_text("unchanged\n", encoding="utf-8")
    original_mode = stat.S_IMODE(path.stat().st_mode)
    current_uid = os.geteuid()
    monkeypatch.setattr(private_write.os, "geteuid", lambda: current_uid + 1)

    with pytest.raises(PermissionError, match="owner mismatch"):
        if operation == "append":
            private_write.append_private_text(path, "private\n", root=root)
        else:
            with private_write.private_file_lock(path, root=root):
                pass

    assert path.read_text(encoding="utf-8") == "unchanged\n"
    assert stat.S_IMODE(path.stat().st_mode) == original_mode


@pytest.mark.skipif(os.name == "nt" or not hasattr(os, "mkfifo"), reason="POSIX FIFO")
@pytest.mark.parametrize("operation", ["append", "lock"])
def test_private_mutation_rejects_fifo_without_blocking(
    tmp_path: Path,
    operation: str,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    fifo = root / "state.fifo"
    os.mkfifo(fifo)

    with pytest.raises(OSError, match="regular file"):
        if operation == "append":
            private_write.append_private_text(fifo, "private\n", root=root)
        else:
            with private_write.private_file_lock(fifo, root=root):
                pass


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory permissions")
@pytest.mark.parametrize("operation", ["replace", "append", "read", "lock"])
def test_private_operations_reject_group_writable_parent_component(
    tmp_path: Path,
    operation: str,
) -> None:
    root = tmp_path / "project"
    parent = root / ".ai"
    parent.mkdir(parents=True)
    parent.chmod(0o777)
    path = parent / "state.txt"
    path.write_text("existing\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)

    with pytest.raises(PermissionError, match="group/other writable"):
        if operation == "replace":
            private_write.atomic_write_private_text(path, "private\n", root=root)
        elif operation == "append":
            private_write.append_private_text(path, "private\n", root=root)
        elif operation == "read":
            private_write.read_root_confined_text(path, root=root)
        else:
            with private_write.private_file_lock(path, root=root):
                pass

    assert path.read_text(encoding="utf-8") == "existing\n"
    assert stat.S_IMODE(parent.stat().st_mode) == 0o777