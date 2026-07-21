from __future__ import annotations

import errno
import os
import secrets
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]


def _require_root_confined_parent(path: Path, root: Path | None) -> None:
    if root is None:
        return
    try:
        path.parent.resolve().relative_to(Path(root).resolve())
    except (OSError, ValueError) as exc:
        raise OSError("private write parent escapes project root") from exc


def _confined_parent_parts(path: Path, root: Path) -> tuple[Path, tuple[str, ...]]:
    root_absolute = Path(os.path.abspath(root))
    path_absolute = Path(os.path.abspath(path))
    try:
        relative_parent = path_absolute.parent.relative_to(root_absolute)
    except ValueError as exc:
        raise OSError("private write parent escapes project root") from exc
    parts = tuple(part for part in relative_parent.parts if part not in ("", "."))
    if any(part == ".." for part in parts):
        raise OSError("private write parent escapes project root")
    return root_absolute, parts


def _raise_confined_path_error(exc: OSError) -> None:
    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
        raise OSError("private path contains symlink or escapes project root") from exc
    if exc.errno in {errno.ENXIO, errno.ENODEV}:
        raise OSError("private mutation target is not a regular file") from exc
    raise exc


def _require_safe_directory_fd(fd: int) -> None:
    state = os.fstat(fd)
    if not stat.S_ISDIR(state.st_mode):
        raise OSError("private path component is not a directory")
    if os.name != "nt":
        if stat.S_IMODE(state.st_mode) & 0o022:
            raise PermissionError("private path component is group/other writable")
        if hasattr(os, "geteuid") and state.st_uid != os.geteuid():
            raise PermissionError("private path component owner mismatch")


@contextmanager
def _open_confined_parent_fd(
    path: Path,
    *,
    root: Path,
    create: bool,
) -> Iterator[int]:
    """Open each parent component from root without following directory links."""
    root_absolute, parts = _confined_parent_parts(path, root)
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
    if create:
        root_absolute.mkdir(parents=True, exist_ok=True)
    current_fd = os.open(root_absolute, directory_flags)
    try:
        for part in parts:
            child_flags = directory_flags
            if hasattr(os, "O_NOFOLLOW"):
                child_flags |= os.O_NOFOLLOW
            try:
                child_fd = os.open(part, child_flags, dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, 0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                try:
                    child_fd = os.open(part, child_flags, dir_fd=current_fd)
                except OSError as exc:
                    _raise_confined_path_error(exc)
            except OSError as exc:
                _raise_confined_path_error(exc)
            _require_safe_directory_fd(child_fd)
            os.close(current_fd)
            current_fd = child_fd
        yield current_fd
    finally:
        try:
            os.close(current_fd)
        except OSError:
            pass


def _write_all(fd: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(fd, remaining)
        if written <= 0:
            raise OSError("write made no progress")
        remaining = remaining[written:]


def _open_or_create_private_file(
    parent_fd: int,
    name: str,
    flags: int,
    *,
    retries: int = 4,
) -> int:
    """Race-safe open for a shared private file under an already-confined parent."""
    existing_flags = flags & ~os.O_CREAT
    create_flags = existing_flags | os.O_CREAT | os.O_EXCL
    last_error: OSError | None = None
    for _attempt in range(max(1, retries)):
        try:
            return os.open(name, create_flags, 0o600, dir_fd=parent_fd)
        except FileExistsError:
            try:
                return os.open(name, existing_flags, dir_fd=parent_fd)
            except FileNotFoundError as exc:
                # Another actor replaced the directory entry between the two
                # operations. Retry against the same confined parent fd.
                last_error = exc
                continue
        except FileNotFoundError as exc:
            # Some platforms transiently report ENOENT for concurrent first
            # creation with O_NOFOLLOW. A bounded retry is safe and deterministic.
            last_error = exc
            continue
    if last_error is not None:
        raise last_error
    raise OSError("unable to open or create private file")


def _require_single_link(state: os.stat_result) -> None:
    if int(getattr(state, "st_nlink", 1)) != 1:
        raise OSError("private file has multiple hard links")


def _require_private_mutation_target(state: os.stat_result) -> None:
    if not stat.S_ISREG(state.st_mode):
        raise OSError("private mutation target is not a regular file")
    _require_single_link(state)
    if os.name != "nt" and hasattr(os, "geteuid") and state.st_uid != os.geteuid():
        raise PermissionError("private mutation target owner mismatch")


def ensure_root_confined_directory(
    path: Path,
    *,
    root: Path,
    mode: int = 0o700,
) -> Path:
    """Create/validate a non-symlink directory confined under *root*."""
    path = Path(path)
    root = Path(root)
    if os.name != "nt":
        with _open_confined_parent_fd(path, root=root, create=True) as parent_fd:
            flags = os.O_RDONLY
            if hasattr(os, "O_DIRECTORY"):
                flags |= os.O_DIRECTORY
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                directory_fd = os.open(path.name, flags, dir_fd=parent_fd)
            except FileNotFoundError:
                os.mkdir(path.name, mode, dir_fd=parent_fd)
                directory_fd = os.open(path.name, flags, dir_fd=parent_fd)
            except OSError as exc:
                _raise_confined_path_error(exc)
            try:
                state = os.fstat(directory_fd)
                if not stat.S_ISDIR(state.st_mode):
                    raise OSError("private directory is not a regular directory")
                _require_safe_directory_fd(directory_fd)
                if hasattr(os, "fchmod"):
                    os.fchmod(directory_fd, mode)
            finally:
                os.close(directory_fd)
        return path
    _require_root_confined_parent(path, root)
    if path.is_symlink():
        raise OSError("refusing symlink directory")
    path.mkdir(parents=True, exist_ok=True)
    try:
        state = path.lstat()
        if not stat.S_ISDIR(state.st_mode) or stat.S_ISLNK(state.st_mode):
            raise OSError("private directory is not a regular directory")
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise OSError("private directory escapes project root") from exc
    if os.name != "nt":
        path.chmod(mode)
    return path


def validate_root_confined_directory(
    path: Path,
    *,
    root: Path,
    require_safe_permissions: bool = True,
) -> os.stat_result:
    """Validate an existing directory through root-relative no-follow fds."""
    path = Path(path)
    root = Path(root)
    if os.name != "nt":
        with _open_confined_parent_fd(path, root=root, create=False) as parent_fd:
            flags = os.O_RDONLY
            if hasattr(os, "O_DIRECTORY"):
                flags |= os.O_DIRECTORY
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                directory_fd = os.open(path.name, flags, dir_fd=parent_fd)
            except OSError as exc:
                _raise_confined_path_error(exc)
            try:
                state = os.fstat(directory_fd)
                if not stat.S_ISDIR(state.st_mode):
                    raise OSError("private path component is not a directory")
                if require_safe_permissions:
                    _require_safe_directory_fd(directory_fd)
                return state
            finally:
                os.close(directory_fd)
    _require_root_confined_parent(path, root)
    if path.is_symlink():
        raise OSError("refusing symlink directory")
    state = path.stat()
    if not stat.S_ISDIR(state.st_mode):
        raise OSError("private path component is not a directory")
    return state


def atomic_write_private_text(path: Path, text: str, *, root: Path | None = None) -> None:
    """Atomically replace *path* with UTF-8 text created as mode 0600.

    The temporary file is private before the first byte is written. Replacing an
    existing symlink replaces the link itself and never follows its target.
    """
    path = Path(path)
    if root is not None and os.name != "nt":
        encoded = text.encode("utf-8")
        with _open_confined_parent_fd(path, root=Path(root), create=True) as parent_fd:
            temporary_name = f".{path.name}.{secrets.token_hex(8)}.tmp"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(temporary_name, flags, 0o600, dir_fd=parent_fd)
            try:
                if hasattr(os, "fchmod"):
                    os.fchmod(fd, 0o600)
                _write_all(fd, encoded)
                os.fsync(fd)
                os.close(fd)
                fd = -1
                os.replace(
                    temporary_name,
                    path.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                try:
                    os.fsync(parent_fd)
                except OSError:
                    pass
            finally:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
        return
    _require_root_confined_parent(path, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def append_private_text(path: Path, text: str, *, root: Path | None = None) -> None:
    """Append one UTF-8 record to a private regular file without following links."""
    path = Path(path)
    if root is not None and os.name != "nt":
        with _open_confined_parent_fd(path, root=Path(root), create=True) as parent_fd:
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
            if hasattr(os, "O_NONBLOCK"):
                flags |= os.O_NONBLOCK
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                fd = _open_or_create_private_file(parent_fd, path.name, flags)
            except OSError as exc:
                _raise_confined_path_error(exc)
            try:
                _require_private_mutation_target(os.fstat(fd))
                if hasattr(os, "fchmod"):
                    os.fchmod(fd, 0o600)
                encoded = text.encode("utf-8")
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                _write_all(fd, encoded)
            finally:
                if fcntl is not None:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                os.close(fd)
        return
    _require_root_confined_parent(path, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise OSError("refusing to append through symlink")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        _require_private_mutation_target(os.fstat(fd))
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        encoded = text.encode("utf-8")
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        remaining = memoryview(encoded)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError("append made no progress")
            remaining = remaining[written:]
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)
    if os.name != "nt":
        path.chmod(0o600)


def read_root_confined_text(
    path: Path,
    *,
    root: Path | None = None,
    max_bytes: int = 1_000_000,
    require_private: bool = True,
) -> tuple[str, os.stat_result]:
    """Read a bounded regular file without following the final symlink."""
    path = Path(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    parent_context = (
        _open_confined_parent_fd(path, root=Path(root), create=False)
        if root is not None and os.name != "nt"
        else None
    )
    if parent_context is not None:
        with parent_context as parent_fd:
            try:
                fd = os.open(path.name, flags, dir_fd=parent_fd)
            except OSError as exc:
                _raise_confined_path_error(exc)
            return _read_open_text_fd(
                fd,
                max_bytes=max_bytes,
                require_private=require_private,
            )
    _require_root_confined_parent(path, root)
    if path.is_symlink():
        raise OSError("refusing to read through symlink")
    fd = os.open(path, flags)
    return _read_open_text_fd(
        fd,
        max_bytes=max_bytes,
        require_private=require_private,
    )


def read_root_confined_tail_text(
    path: Path,
    *,
    root: Path | None = None,
    max_bytes: int = 1_000_000,
    require_private: bool = True,
) -> tuple[str, os.stat_result, dict[str, int | bool]]:
    """Read a bounded UTF-8 tail without following links or splitting a line.

    The read is pinned to the size observed from the opened descriptor, so a
    concurrent append cannot turn a bounded recall/status operation into an
    unbounded stream. When the file is larger than ``max_bytes``, the partial
    first line is discarded and ``partial`` is reported explicitly.
    """
    path = Path(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    parent_context = (
        _open_confined_parent_fd(path, root=Path(root), create=False)
        if root is not None and os.name != "nt"
        else None
    )
    if parent_context is not None:
        with parent_context as parent_fd:
            try:
                fd = os.open(path.name, flags, dir_fd=parent_fd)
            except OSError as exc:
                _raise_confined_path_error(exc)
            return _read_open_tail_text_fd(
                fd,
                max_bytes=max_bytes,
                require_private=require_private,
            )
    _require_root_confined_parent(path, root)
    if path.is_symlink():
        raise OSError("refusing to read through symlink")
    fd = os.open(path, flags)
    return _read_open_tail_text_fd(
        fd,
        max_bytes=max_bytes,
        require_private=require_private,
    )


def _read_open_tail_text_fd(
    fd: int,
    *,
    max_bytes: int,
    require_private: bool,
) -> tuple[str, os.stat_result, dict[str, int | bool]]:
    try:
        state = os.fstat(fd)
        if not stat.S_ISREG(state.st_mode):
            raise OSError("private read target is not a regular file")
        _require_single_link(state)
        if require_private and os.name != "nt":
            if stat.S_IMODE(state.st_mode) & 0o077:
                raise PermissionError("private read target has group/other permissions")
            if hasattr(os, "geteuid") and state.st_uid != os.geteuid():
                raise PermissionError("private read target owner mismatch")

        limit = max(0, int(max_bytes))
        file_bytes = max(0, int(state.st_size))
        start = max(0, file_bytes - limit)
        os.lseek(fd, start, os.SEEK_SET)
        remaining = file_bytes - start
        chunks: list[bytes] = []
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        dropped_partial_line_bytes = 0
        if start > 0:
            newline = raw.find(b"\n")
            if newline < 0:
                dropped_partial_line_bytes = len(raw)
                raw = b""
            else:
                dropped_partial_line_bytes = newline + 1
                raw = raw[newline + 1 :]
        final_state = os.fstat(fd)
        source_changed = (
            int(final_state.st_dev) != int(state.st_dev)
            or int(final_state.st_ino) != int(state.st_ino)
            or int(final_state.st_size) != file_bytes
        )
        metadata: dict[str, int | bool] = {
            "file_bytes": file_bytes,
            "bytes_read": sum(len(chunk) for chunk in chunks),
            "bytes_returned": len(raw),
            "omitted_prefix_bytes": start + dropped_partial_line_bytes,
            "dropped_partial_line_bytes": dropped_partial_line_bytes,
            "source_changed": source_changed,
            "partial": bool(start > 0 or source_changed),
        }
        return raw.decode("utf-8"), state, metadata
    finally:
        os.close(fd)


def _read_open_text_fd(
    fd: int,
    *,
    max_bytes: int,
    require_private: bool,
) -> tuple[str, os.stat_result]:
    try:
        state = os.fstat(fd)
        if not stat.S_ISREG(state.st_mode):
            raise OSError("private read target is not a regular file")
        _require_single_link(state)
        if require_private and os.name != "nt":
            if stat.S_IMODE(state.st_mode) & 0o077:
                raise PermissionError("private read target has group/other permissions")
            if hasattr(os, "geteuid") and state.st_uid != os.geteuid():
                raise PermissionError("private read target owner mismatch")
        limit = max(0, int(max_bytes))
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(65536, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise OSError(f"private read exceeds {limit} bytes")
        return b"".join(chunks).decode("utf-8"), state
    finally:
        os.close(fd)


@contextmanager
def private_file_lock(path: Path, *, root: Path | None = None) -> Iterator[None]:
    """Hold a cross-platform exclusive lock on a private, confined lock file."""
    path = Path(path)
    if root is not None and os.name != "nt":
        with _open_confined_parent_fd(path, root=Path(root), create=True) as parent_fd:
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_NONBLOCK"):
                flags |= os.O_NONBLOCK
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                fd = _open_or_create_private_file(parent_fd, path.name, flags)
            except OSError as exc:
                _raise_confined_path_error(exc)
            handle = os.fdopen(fd, "r+b", buffering=0)
            try:
                _require_private_mutation_target(os.fstat(handle.fileno()))
                if hasattr(os, "fchmod"):
                    os.fchmod(handle.fileno(), 0o600)
                if os.fstat(handle.fileno()).st_size == 0:
                    handle.write(b"\0")
                handle.seek(0)
                _lock_handle_required(handle)
                yield
            finally:
                _unlock_handle_required(handle)
                handle.close()
        return
    _require_root_confined_parent(path, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise OSError("refusing to lock through symlink")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    handle = os.fdopen(fd, "r+b", buffering=0)
    try:
        _require_private_mutation_target(os.fstat(handle.fileno()))
        if hasattr(os, "fchmod"):
            os.fchmod(handle.fileno(), 0o600)
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"\0")
        handle.seek(0)
        _lock_handle_required(handle)
        yield
    finally:
        _unlock_handle_required(handle)
        handle.close()
    if os.name != "nt":
        path.chmod(0o600)


def _lock_handle_required(handle) -> None:
    if os.name == "nt":
        try:
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            return
        except (ImportError, OSError) as exc:
            raise OSError("exclusive file locking unavailable") from exc
    if fcntl is None:
        raise OSError("exclusive file locking unavailable")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except OSError as exc:
        raise OSError("exclusive file locking unavailable") from exc


def _unlock_handle_required(handle) -> None:
    if os.name == "nt":
        try:
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except (ImportError, OSError):
            pass
        return
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
