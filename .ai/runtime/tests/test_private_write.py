from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from ai_core import private_write


def test_atomic_private_write_creates_expected_content_and_mode(tmp_path: Path) -> None:
    path = tmp_path / "cache" / "state.json"

    private_write.atomic_write_private_text(path, '{"ok":true}\n')

    assert path.read_text(encoding="utf-8") == '{"ok":true}\n'
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_ensure_private_regular_file_preserves_content_and_repairs_mode(tmp_path: Path) -> None:
    root = tmp_path / "project"
    path = root / ".ai" / "cache" / "state.bin"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"preserved")
    if os.name != "nt":
        path.chmod(0o644)

    state = private_write.ensure_root_confined_private_regular_file(
        path,
        root=root,
    )

    assert path.read_bytes() == b"preserved"
    assert state.st_nlink == 1
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="Unix link semantics")
@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_ensure_private_regular_file_replaces_link_without_external_mutation(
    tmp_path: Path,
    link_kind: str,
) -> None:
    root = tmp_path / "project"
    path = root / ".ai" / "cache" / "state.bin"
    path.parent.mkdir(parents=True)
    external = tmp_path / f"external-{link_kind}.bin"
    external.write_bytes(b"external")
    external.chmod(0o600)
    if link_kind == "symlink":
        path.symlink_to(external)
    else:
        os.link(external, path)

    state = private_write.ensure_root_confined_private_regular_file(
        path,
        root=root,
        replace_unsafe=True,
    )

    assert external.read_bytes() == b"external"
    assert path.read_bytes() == b""
    assert not path.is_symlink()
    assert state.st_nlink == 1
    assert path.stat().st_ino != external.stat().st_ino
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


def test_private_ttl_marker_claim_is_recently_idempotent_and_private(tmp_path: Path) -> None:
    root = tmp_path / "project"
    marker = root / ".ai" / "cache" / "model" / ".install-lock"

    assert private_write.claim_private_ttl_marker(marker, root=root, ttl_seconds=3600) is True
    assert private_write.claim_private_ttl_marker(marker, root=root, ttl_seconds=3600) is False
    assert marker.read_text(encoding="utf-8") == "running"
    if os.name != "nt":
        assert stat.S_IMODE(marker.stat().st_mode) == 0o600
        assert marker.stat().st_nlink == 1


def test_private_ttl_marker_tolerates_small_future_clock_skew(tmp_path: Path) -> None:
    root = tmp_path / "project"
    marker = root / ".ai" / "cache" / "model" / ".install-lock"
    marker.parent.mkdir(parents=True)
    marker.write_text("running", encoding="utf-8")
    if os.name != "nt":
        marker.chmod(0o600)
    os.utime(marker, (1030.0, 1030.0))

    claimed = private_write.claim_private_ttl_marker(
        marker,
        root=root,
        ttl_seconds=3600,
        now=1000.0,
        max_future_skew_seconds=60,
    )

    assert claimed is False
    assert marker.stat().st_mtime == pytest.approx(1030.0)


@pytest.mark.parametrize("future_offset", [301.0, 3600.0, 86_400.0])
def test_private_ttl_marker_reclaims_far_future_mtime(
    tmp_path: Path,
    future_offset: float,
) -> None:
    root = tmp_path / "project"
    marker = root / ".ai" / "cache" / "model" / ".install-lock"
    marker.parent.mkdir(parents=True)
    marker.write_text("future-lock", encoding="utf-8")
    if os.name != "nt":
        marker.chmod(0o600)
    os.utime(marker, (1000.0 + future_offset, 1000.0 + future_offset))

    claimed = private_write.claim_private_ttl_marker(
        marker,
        root=root,
        ttl_seconds=3600,
        now=1000.0,
        max_future_skew_seconds=300,
    )

    assert claimed is True
    assert marker.read_text(encoding="utf-8") == "running"
    assert marker.stat().st_mtime != pytest.approx(1000.0 + future_offset)


@pytest.mark.parametrize("ttl_seconds", [0.0, -1.0])
def test_private_ttl_marker_nonpositive_ttl_always_reclaims(
    tmp_path: Path,
    ttl_seconds: float,
) -> None:
    root = tmp_path / "project"
    marker = root / ".ai" / "cache" / "model" / ".install-lock"
    marker.parent.mkdir(parents=True)
    marker.write_text("old", encoding="utf-8")
    if os.name != "nt":
        marker.chmod(0o600)
    os.utime(marker, (1000.0, 1000.0))

    assert private_write.claim_private_ttl_marker(
        marker,
        root=root,
        ttl_seconds=ttl_seconds,
        now=1000.0,
    ) is True
    assert marker.read_text(encoding="utf-8") == "running"


def test_private_ttl_marker_release_requires_matching_owner(tmp_path: Path) -> None:
    root = tmp_path / "project"
    marker = root / ".ai" / "cache" / "model" / ".install-lock"
    assert private_write.claim_private_ttl_marker(
        marker,
        root=root,
        ttl_seconds=3600,
        text="current-owner",
    ) is True

    assert private_write.release_private_ttl_marker(
        marker,
        root=root,
        expected_text="old-owner",
    ) is False
    assert marker.read_text(encoding="utf-8") == "current-owner"
    assert private_write.release_private_ttl_marker(
        marker,
        root=root,
        expected_text="current-owner",
    ) is True
    assert not marker.exists()
    assert private_write.release_private_ttl_marker(
        marker,
        root=root,
        expected_text="current-owner",
    ) is False


@pytest.mark.skipif(os.name == "nt", reason="Unix link semantics")
@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_private_ttl_marker_release_ignores_linked_external_target(
    tmp_path: Path,
    link_kind: str,
) -> None:
    root = tmp_path / "project"
    marker = root / ".ai" / "cache" / "model" / ".install-lock"
    marker.parent.mkdir(parents=True)
    external = tmp_path / f"external-release-{link_kind}.txt"
    external.write_text("owned-token", encoding="utf-8")
    external.chmod(0o600)
    if link_kind == "symlink":
        marker.symlink_to(external)
    else:
        os.link(external, marker)

    assert private_write.release_private_ttl_marker(
        marker,
        root=root,
        expected_text="owned-token",
    ) is False

    assert external.read_text(encoding="utf-8") == "owned-token"
    assert marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="Unix link semantics")
@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_private_ttl_marker_replaces_link_without_mutating_external_target(
    tmp_path: Path,
    link_kind: str,
) -> None:
    root = tmp_path / "project"
    marker = root / ".ai" / "cache" / "model" / ".install-lock"
    marker.parent.mkdir(parents=True)
    external = tmp_path / f"external-{link_kind}.txt"
    external.write_text("external\n", encoding="utf-8")
    external.chmod(0o600)
    if link_kind == "symlink":
        marker.symlink_to(external)
    else:
        os.link(external, marker)

    assert private_write.claim_private_ttl_marker(marker, root=root, ttl_seconds=3600) is True

    assert external.read_text(encoding="utf-8") == "external\n"
    assert not marker.is_symlink()
    assert marker.read_text(encoding="utf-8") == "running"
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert marker.stat().st_nlink == 1


@pytest.mark.skipif(os.name == "nt", reason="Unix mode semantics")
def test_private_ttl_marker_replaces_public_marker_with_private_state(tmp_path: Path) -> None:
    root = tmp_path / "project"
    marker = root / ".ai" / "cache" / "model" / ".install-lock"
    marker.parent.mkdir(parents=True)
    marker.write_text("untrusted\n", encoding="utf-8")
    marker.chmod(0o644)

    assert private_write.claim_private_ttl_marker(marker, root=root, ttl_seconds=3600) is True

    assert marker.read_text(encoding="utf-8") == "running"
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="Unix directory symlink semantics")
def test_private_ttl_marker_rejects_external_parent_symlink(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (root / ".ai").symlink_to(external, target_is_directory=True)
    marker = root / ".ai" / "cache" / "model" / ".install-lock"

    with pytest.raises(OSError, match="escapes project root"):
        private_write.claim_private_ttl_marker(marker, root=root, ttl_seconds=3600)

    assert not (external / "cache" / "model" / ".install-lock").exists()


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


def test_remove_root_confined_tree_removes_nested_tree_and_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "project"
    target = root / ".ai" / "cache" / "model"
    nested = target / "nested" / "artifact.bin"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"artifact")

    assert private_write.remove_root_confined_tree(target, root=root) is True
    assert not target.exists()
    assert private_write.remove_root_confined_tree(target, root=root) is False


@pytest.mark.skipif(os.name == "nt", reason="Unix symlink semantics")
def test_remove_root_confined_tree_rejects_final_symlink(tmp_path: Path) -> None:
    root = tmp_path / "project"
    target = root / ".ai" / "cache" / "model"
    target.parent.mkdir(parents=True)
    external = tmp_path / "external-model"
    external.mkdir()
    payload = external / "artifact.bin"
    payload.write_bytes(b"external")
    target.symlink_to(external, target_is_directory=True)

    with pytest.raises(OSError, match="regular directory"):
        private_write.remove_root_confined_tree(target, root=root)

    assert payload.read_bytes() == b"external"
    assert target.is_symlink()


@pytest.mark.skipif(os.name == "nt", reason="Unix directory symlink semantics")
def test_remove_root_confined_tree_rejects_external_parent_symlink(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    external = tmp_path / "external"
    target = external / "cache" / "model"
    target.mkdir(parents=True)
    payload = target / "artifact.bin"
    payload.write_bytes(b"external")
    (root / ".ai").symlink_to(external, target_is_directory=True)

    with pytest.raises(OSError, match="symlink|escapes"):
        private_write.remove_root_confined_tree(root / ".ai" / "cache" / "model", root=root)

    assert payload.read_bytes() == b"external"


@pytest.mark.skipif(os.name == "nt", reason="Unix child symlink semantics")
def test_remove_root_confined_tree_unlinks_child_links_without_following(tmp_path: Path) -> None:
    root = tmp_path / "project"
    target = root / ".ai" / "cache" / "model"
    target.mkdir(parents=True)
    external_dir = tmp_path / "external-dir"
    external_dir.mkdir()
    external_file = external_dir / "artifact.bin"
    external_file.write_bytes(b"external")
    (target / "linked-dir").symlink_to(external_dir, target_is_directory=True)
    os.link(external_file, target / "linked-file")

    assert private_write.remove_root_confined_tree(target, root=root) is True

    assert not target.exists()
    assert external_file.read_bytes() == b"external"
    assert external_file.stat().st_nlink == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX fd-based removal")
def test_remove_root_confined_tree_fails_closed_without_safe_rmtree(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "project"
    target = root / ".ai" / "cache" / "model"
    target.mkdir(parents=True)
    payload = target / "artifact.bin"
    payload.write_bytes(b"preserve")
    monkeypatch.setattr(private_write.shutil.rmtree, "avoids_symlink_attacks", False)

    with pytest.raises(OSError, match="secure recursive removal"):
        private_write.remove_root_confined_tree(target, root=root)

    assert payload.read_bytes() == b"preserve"


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable semantics")
def test_root_confined_executable_accepts_owned_single_link_launcher(tmp_path: Path) -> None:
    root = tmp_path / "project"
    launcher = root / ".ai" / "bin" / "ai"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)

    state = private_write.validate_root_confined_executable(launcher, root=root)

    assert stat.S_ISREG(state.st_mode)
    assert state.st_nlink == 1


@pytest.mark.skipif(os.name == "nt", reason="Unix link semantics")
@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_root_confined_executable_rejects_linked_launcher(
    tmp_path: Path,
    link_kind: str,
) -> None:
    root = tmp_path / "project"
    launcher = root / ".ai" / "bin" / "ai"
    launcher.parent.mkdir(parents=True)
    external = tmp_path / f"external-{link_kind}"
    external.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    external.chmod(0o755)
    if link_kind == "symlink":
        launcher.symlink_to(external)
    else:
        os.link(external, launcher)

    with pytest.raises(OSError):
        private_write.validate_root_confined_executable(launcher, root=root)


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable semantics")
@pytest.mark.parametrize(
    ("mode", "message"),
    [(0o644, "not executable"), (0o777, "group/other writable")],
)
def test_root_confined_executable_rejects_unsafe_mode(
    tmp_path: Path,
    mode: int,
    message: str,
) -> None:
    root = tmp_path / "project"
    launcher = root / ".ai" / "bin" / "ai"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(mode)

    with pytest.raises(PermissionError, match=message):
        private_write.validate_root_confined_executable(launcher, root=root)


@pytest.mark.skipif(os.name == "nt" or not hasattr(os, "mkfifo"), reason="POSIX FIFO")
def test_root_confined_executable_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    root = tmp_path / "project"
    launcher = root / ".ai" / "bin" / "ai"
    launcher.parent.mkdir(parents=True)
    os.mkfifo(launcher)

    with pytest.raises(OSError, match="regular file"):
        private_write.validate_root_confined_executable(launcher, root=root)


@pytest.mark.skipif(os.name == "nt", reason="Unix directory symlink semantics")
def test_root_confined_executable_rejects_external_parent_symlink(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    external = tmp_path / "external"
    (external / "bin").mkdir(parents=True)
    launcher = external / "bin" / "ai"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    (root / ".ai").symlink_to(external, target_is_directory=True)

    with pytest.raises(OSError, match="escapes project root"):
        private_write.validate_root_confined_executable(root / ".ai" / "bin" / "ai", root=root)


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